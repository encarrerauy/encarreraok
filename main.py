# EncarreraOK - MVP de deslindes digitales
#
# Requisitos del MVP:
# - FastAPI + Uvicorn (sirve bajo systemd)
# - Nginx como reverse proxy (ya configurado)
# - SQLite para persistencia
# - HTML mínimo renderizado con Jinja2
# - Sin frameworks extra ni ORM (sqlite3 estándar)
#
# Este archivo `main.py` es autocontenido para el MVP:
# - Inicializa la base SQLite y crea las tablas si no existen
# - Define los modelos de datos (Pydantic) para claridad tipada
# - Expone endpoints:
#     GET  /e/{evento_id}        -> Formulario de aceptación
#     POST /e/{evento_id}        -> Guarda aceptación y confirma
#     GET  /admin/aceptaciones   -> Lista aceptaciones (sin auth)
# - Renderiza HTML con Jinja2 usando plantillas en memoria
#
# Notas:
# - En producción, se recomienda mover las plantillas a /var/www/encarreraok/app/templates
#   y reemplazar el DictLoader por FileSystemLoader.
# - Ruta de la base: configurable con ENV `ENCARRERAOK_DB_PATH`.

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from jinja2 import Environment, DictLoader, select_autoescape
from pydantic import BaseModel
from datetime import datetime, date
import sqlite3
import os
import stat
import hashlib
import re
import base64
import uuid
import shutil
from typing import Optional, List, Dict, Any
import io
import logging
import traceback
from logging.handlers import RotatingFileHandler

# Intentar importar PIL para compresión de imágenes (opcional)
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ------------------------------------------------------------------------------
# Configuración de logging
# ------------------------------------------------------------------------------
LOG_DIR = "/var/log/encarreraok"
LOG_FILE = os.path.join(LOG_DIR, "app.log")

def setup_logging() -> None:
    """Configura logging a archivo con rotación."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        # Fallback: usar directorio actual si no se puede crear /var/log
        LOG_DIR = os.path.dirname(os.path.abspath(__file__))
        LOG_FILE = os.path.join(LOG_DIR, "app.log")
    
    # Handler con rotación (10MB, 5 backups)
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    
    logger = logging.getLogger('encarreraok')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger

app_logger = setup_logging()


# ------------------------------------------------------------------------------
# Constantes
# ------------------------------------------------------------------------------
# Límites de tamaño por tipo de evidencia (prevención 413)
MAX_IMAGE_DOC_MB = 4  # Imagen documento: máx 4 MB por archivo
MAX_FIRMA_MB = 1      # Firma canvas: máx 1 MB
MAX_AUDIO_MB = 5      # Audio: máx 5 MB
# Límites para compresión automática
MAX_IMAGE_COMPRESS_THRESHOLD_MB = 2  # Si supera esto, comprimir
MAX_IMAGE_COMPRESS_TARGET_MB = 1.5   # Objetivo después de compresión

DESLINDE_TEXTO_BASE = """DESLINDE DE RESPONSABILIDAD Y ACEPTACIÓN DE RIESGOS

Declaro que participo en el evento deportivo {{NOMBRE_EVENTO}}, organizado por {{ORGANIZADOR}}, de manera voluntaria y bajo mi exclusiva responsabilidad.

Reconozco que la participación en actividades deportivas implica riesgos inherentes, incluyendo, pero no limitándose a, caídas, lesiones físicas, traumatismos, accidentes cardiovasculares, condiciones climáticas adversas y otros riesgos propios de la actividad.

Declaro encontrarme en condiciones físicas y de salud adecuadas para participar, y que he sido debidamente informado/a sobre las características del evento.

Eximo de toda responsabilidad civil, penal y administrativa al organizador, auspiciantes, colaboradores, personal médico, autoridades y cualquier otra persona vinculada a la organización del evento, por cualquier daño, lesión o perjuicio que pudiera sufrir antes, durante o después de mi participación.

Autorizo la utilización de mi imagen, voz y datos personales con fines de difusión, promoción y registro del evento, sin derecho a compensación económica.

Declaro haber leído, comprendido y aceptado íntegramente el presente deslinde de responsabilidad."""


# ------------------------------------------------------------------------------
# Configuración de aplicación y plantillas Jinja2 (en memoria para el MVP)
# ------------------------------------------------------------------------------
app = FastAPI(title="EncarreraOK - MVP deslindes")

templates_env = Environment(
    loader=DictLoader(
        {
            # Plantilla de formulario de aceptación de deslinde
            "evento_form.html": """
            <!doctype html>
            <html lang="es">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>{{ evento.nombre }} - Deslinde</title>
                <style>
                    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
                    .card { max-width: 640px; margin: 0 auto; padding: 24px; border: 1px solid #ddd; border-radius: 8px; }
                    label { display: block; margin: 12px 0 4px; }
                    input[type="text"] { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 4px; }
                    .checkbox { margin-top: 16px; }
                    .btn { margin-top: 16px; padding: 10px 16px; border: none; background: #0d6efd; color: white; border-radius: 4px; cursor: pointer; }
                    .btn:disabled { background: #aaa; cursor: not-allowed; }
                    .muted { color: #666; font-size: 0.95em; }
                    .deslinde { white-space: pre-wrap; background: #fafafa; border: 1px solid #eee; padding: 12px; border-radius: 6px; margin-top: 12px; }
                    
                    /* Firma Canvas */
                    .signature-pad { border: 1px solid #ccc; border-radius: 4px; touch-action: none; background: #fff; width: 100%; height: 200px; margin-top: 8px; }
                    .signature-container { margin-top: 16px; }
                    .btn-clear { background: #6c757d; font-size: 0.9em; padding: 6px 12px; margin-top: 4px; }
                    
                    /* Documentos */
                    .doc-container { margin-top: 16px; border: 1px solid #eee; padding: 12px; border-radius: 6px; background: #fdfdfd; }
                    .doc-container h3 { margin-top: 0; font-size: 1.1em; color: #444; }
                    .file-input-group { margin-bottom: 12px; }
                    .file-input-group label { display: block; margin-bottom: 4px; font-weight: bold; }
                    .file-hint { font-size: 0.85em; color: #777; margin-top: 2px; }
                    .file-feedback { font-size: 0.85em; margin-top: 4px; padding: 6px; border-radius: 4px; }
                    .file-feedback.warning { background: #fff3cd; color: #856404; border: 1px solid #ffc107; }
                    .file-feedback.info { background: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
                    .file-feedback.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
                    
                    /* Audio */
                    .audio-container { margin-top: 16px; border: 1px solid #eee; padding: 12px; border-radius: 6px; background: #fdfdfd; }
                    .audio-controls { display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
                    .audio-status { margin-left: 8px; font-size: 0.9em; color: #555; }
                    .audio-text { font-style: italic; background: #eee; padding: 8px; border-radius: 4px; margin-bottom: 8px; color: #333; font-size: 0.95em; }
                    .btn-record { background: #d9534f; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; }
                    .btn-stop { background: #333; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; }
                    .btn-play { background: #0d6efd; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; }
                    .btn-reset { background: #6c757d; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; }
                    .btn-record:disabled, .btn-stop:disabled, .btn-play:disabled, .btn-reset:disabled { background: #ccc; cursor: not-allowed; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>{{ evento.nombre }}</h1>
                    <p class="muted">
                        Fecha: {{ evento.fecha|fecha_ddmmaaaa }}<br/>
                        Organizador: {{ evento.organizador }}
                    </p>
                    <div class="deslinde">
                        {{ deslinde_texto }}
                    </div>
                    {% if not evento.activo %}
                        <p>Este evento no está activo.</p>
                    {% else %}
                        <form method="post" action="{{ request.url.path }}" id="acceptForm" enctype="multipart/form-data">
                            <label for="nombre_participante">Nombre del participante</label>
                            <input type="text" id="nombre_participante" name="nombre_participante" required />

                            <label for="documento">Documento</label>
                            <input type="text" id="documento" name="documento" required />

                            {% if evento.req_documento %}
                            <div class="doc-container">
                                <h3>Documento de Identidad</h3>
                                <div class="file-input-group">
                                    <label for="doc_frente">Frente del documento</label>
                                    <input type="file" id="doc_frente" name="doc_frente" accept="image/*" capture="environment" required>
                                    <div class="file-hint">Foto clara del frente (cámara o archivo). Máx. {{ MAX_IMAGE_DOC_MB }} MB</div>
                                    <div id="doc_frente_feedback" class="file-feedback" style="display:none;"></div>
                                    <div id="doc_frente_mobile_tip" class="file-feedback info" style="display:none;">
                                        📱 <strong>Modo documento:</strong> Use la cámara trasera para mejor calidad. Asegúrese de que el documento esté bien iluminado y completo.
                                    </div>
                                </div>
                                <div class="file-input-group">
                                    <label for="doc_dorso">Dorso del documento</label>
                                    <input type="file" id="doc_dorso" name="doc_dorso" accept="image/*" capture="environment" required>
                                    <div class="file-hint">Foto clara del dorso (cámara o archivo). Máx. {{ MAX_IMAGE_DOC_MB }} MB</div>
                                    <div id="doc_dorso_feedback" class="file-feedback" style="display:none;"></div>
                                    <div id="doc_dorso_mobile_tip" class="file-feedback info" style="display:none;">
                                        📱 <strong>Modo documento:</strong> Use la cámara trasera para mejor calidad. Asegúrese de que el documento esté bien iluminado y completo.
                                    </div>
                                </div>
                            </div>
                            <script>
                                (function() {
                                    // Detección mobile
                                    function isMobile() {
                                        return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) || 
                                               (window.innerWidth <= 768);
                                    }
                                    
                                    // Mostrar tips solo en mobile
                                    if (isMobile()) {
                                        var docFrenteTip = document.getElementById('doc_frente_mobile_tip');
                                        var docDorsoTip = document.getElementById('doc_dorso_mobile_tip');
                                        if (docFrenteTip) docFrenteTip.style.display = 'block';
                                        if (docDorsoTip) docDorsoTip.style.display = 'block';
                                    }
                                    
                                    // Validación tamaño de imágenes (Frontend)
                                    var MAX_IMAGE_BYTES = {{ MAX_IMAGE_DOC_MB }} * 1024 * 1024;
                                    var COMPRESS_THRESHOLD_BYTES = {{ MAX_IMAGE_COMPRESS_THRESHOLD_MB }} * 1024 * 1024;
                                    
                                    function showFeedback(inputId, message, type) {
                                        var feedback = document.getElementById(inputId + '_feedback');
                                        if (feedback) {
                                            feedback.textContent = message;
                                            feedback.className = 'file-feedback ' + type;
                                            feedback.style.display = 'block';
                                        }
                                    }
                                    
                                    function hideFeedback(inputId) {
                                        var feedback = document.getElementById(inputId + '_feedback');
                                        if (feedback) {
                                            feedback.style.display = 'none';
                                        }
                                    }
                                    
                                    function validateFile(input, inputId) {
                                        if (input.files && input.files[0]) {
                                            var size = input.files[0].size;
                                            if (size > MAX_IMAGE_BYTES) {
                                                showFeedback(inputId, "⚠️ Imagen demasiado grande. Máximo permitido: {{ MAX_IMAGE_DOC_MB }} MB.", "error");
                                                input.value = "";
                                                return false;
                                            } else if (size > COMPRESS_THRESHOLD_BYTES) {
                                                showFeedback(inputId, "ℹ️ Imagen grande. Se comprimirá automáticamente al enviar.", "info");
                                            } else {
                                                hideFeedback(inputId);
                                            }
                                        }
                                        return true;
                                    }
                                    
                                    // Validación previa al submit
                                    var form = document.getElementById('acceptForm');
                                    if (form) {
                                        form.addEventListener('submit', function(e) {
                                            var hasError = false;
                                            
                                            // Validar doc_frente
                                            var docFrente = document.getElementById('doc_frente');
                                            if (docFrente && docFrente.files && docFrente.files[0]) {
                                                if (docFrente.files[0].size > MAX_IMAGE_BYTES) {
                                                    showFeedback('doc_frente', "⚠️ El archivo del frente es demasiado grande. Máximo: {{ MAX_IMAGE_DOC_MB }} MB. Por favor seleccione otro archivo.", "error");
                                                    hasError = true;
                                                }
                                            }
                                            
                                            // Validar doc_dorso
                                            var docDorso = document.getElementById('doc_dorso');
                                            if (docDorso && docDorso.files && docDorso.files[0]) {
                                                if (docDorso.files[0].size > MAX_IMAGE_BYTES) {
                                                    showFeedback('doc_dorso', "⚠️ El archivo del dorso es demasiado grande. Máximo: {{ MAX_IMAGE_DOC_MB }} MB. Por favor seleccione otro archivo.", "error");
                                                    hasError = true;
                                                }
                                            }
                                            
                                            if (hasError) {
                                                e.preventDefault();
                                                alert("Por favor corrija los errores antes de enviar. Algunos archivos exceden el tamaño máximo permitido.");
                                                return false;
                                            }
                                        });
                                    }

                                    var docFrente = document.getElementById('doc_frente');
                                    var docDorso = document.getElementById('doc_dorso');
                                    
                                    if (docFrente) {
                                        docFrente.addEventListener('change', function() { validateFile(this, 'doc_frente'); });
                                    }
                                    if (docDorso) {
                                        docDorso.addEventListener('change', function() { validateFile(this, 'doc_dorso'); });
                                    }
                                })();
                            </script>
                            {% endif %}

                            {% if evento.req_audio %}
                            <div class="audio-container">
                                <h3>Audio de aceptación (requerido)</h3>
                                <p>Por favor, grábese leyendo el siguiente texto:</p>
                                <div class="audio-text">
                                    "Yo, <span id="nombre-script">[Nombre]</span>, declaro haber leído y aceptado el deslinde de responsabilidad."
                                </div>
                                
                                <div class="audio-controls">
                                    <button type="button" class="btn-record" id="btn-record">Grabar</button>
                                    <button type="button" class="btn-stop" id="btn-stop" disabled>Detener</button>
                                    <button type="button" class="btn-play" id="btn-play" disabled>Escuchar</button>
                                    <button type="button" class="btn-reset" id="btn-reset" disabled>Regrabar</button>
                                    <span class="audio-status" id="audio-status">Listo para grabar</span>
                                </div>
                                <div id="audio-feedback" class="file-feedback" style="display:none;"></div>
                                <div id="audio_mobile_tip" class="file-feedback info" style="display:none;">
                                    📱 <strong>Nota mobile:</strong> En algunos dispositivos móviles (especialmente iOS) el audio puede no reproducirse localmente, pero la grabación es válida y se guardará correctamente.
                                </div>
                                <audio id="audio-preview" style="display:none"></audio>
                                <input type="hidden" name="audio_base64" id="audio_base64">
                            </div>
                            <script>
                            (function() {
                                // Detección mobile para mostrar tip de audio
                                function isMobile() {
                                    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) || 
                                           (window.innerWidth <= 768);
                                }
                                
                                if (isMobile()) {
                                    var audioTip = document.getElementById('audio_mobile_tip');
                                    if (audioTip) audioTip.style.display = 'block';
                                }
                                
                                var btnRecord = document.getElementById('btn-record');
                                var btnStop = document.getElementById('btn-stop');
                                var btnPlay = document.getElementById('btn-play');
                                var btnReset = document.getElementById('btn-reset');
                                var status = document.getElementById('audio-status');
                                var audioPreview = document.getElementById('audio-preview');
                                var hiddenInput = document.getElementById('audio_base64');
                                var nameInput = document.getElementById('nombre_participante');
                                var nameScript = document.getElementById('nombre-script');
                                
                                // Actualizar nombre en guión
                                if(nameInput) {
                                    nameInput.addEventListener('input', function() {
                                        nameScript.textContent = this.value || "[Nombre]";
                                    });
                                }

                                // Validación tamaño audio (Frontend)
                                var MAX_AUDIO_BYTES = {{ MAX_AUDIO_MB }} * 1024 * 1024;
                                
                                function showAudioFeedback(message, type) {
                                    var feedback = document.getElementById('audio-feedback');
                                    if (feedback) {
                                        feedback.textContent = message;
                                        feedback.className = 'file-feedback ' + type;
                                        feedback.style.display = 'block';
                                    }
                                }
                                
                                function hideAudioFeedback() {
                                    var feedback = document.getElementById('audio-feedback');
                                    if (feedback) {
                                        feedback.style.display = 'none';
                                    }
                                }

                                var mediaRecorder;
                                var audioChunks = [];
                                var audioBlob = null;
                                var stream = null;
                                var canPlayback = false; // Para detectar si el audio es reproducible

                                async function startRecording() {
                                    try {
                                        hideAudioFeedback();
                                        // Detectar soporte de codecs
                                        var mimeType = 'audio/webm';
                                        var codecOptions = ['audio/webm;codecs=opus', 'audio/webm'];
                                        var selectedMime = null;
                                        
                                        for (var i = 0; i < codecOptions.length; i++) {
                                            if (MediaRecorder.isTypeSupported(codecOptions[i])) {
                                                selectedMime = codecOptions[i];
                                                break;
                                            }
                                        }
                                        
                                        if (!selectedMime) {
                                            selectedMime = 'audio/webm'; // Fallback
                                        }
                                        
                                        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                                        mediaRecorder = new MediaRecorder(stream, {
                                            mimeType: selectedMime
                                        });
                                        mediaRecorder.start();
                                        
                                        audioChunks = [];
                                        mediaRecorder.addEventListener("dataavailable", event => {
                                            audioChunks.push(event.data);
                                        });

                                        mediaRecorder.addEventListener("stop", () => {
                                            var mimeType = mediaRecorder.mimeType || 'audio/webm';
                                            audioBlob = new Blob(audioChunks, { type: mimeType });
                                            
                                            // Validar tamaño antes de procesar
                                            if (audioBlob.size > MAX_AUDIO_BYTES) {
                                                showAudioFeedback("⚠️ Audio demasiado grande (máx. {{ MAX_AUDIO_MB }} MB). Por favor, intente ser más breve.", "error");
                                                audioBlob = null;
                                                hiddenInput.value = "";
                                                audioPreview.src = "";
                                                status.textContent = "Audio demasiado grande. Regrabe.";
                                                status.style.color = "red";
                                                canPlayback = false;
                                                
                                                btnRecord.disabled = true;
                                                btnStop.disabled = true;
                                                btnPlay.disabled = true;
                                                btnReset.disabled = false;
                                                return;
                                            }

                                            // Intentar reproducir para detectar compatibilidad (especialmente iOS)
                                            var audioUrl = URL.createObjectURL(audioBlob);
                                            audioPreview.src = audioUrl;
                                            
                                            // Detectar si es iOS
                                            var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
                                            
                                            // Verificar si el audio puede reproducirse
                                            audioPreview.oncanplay = function() {
                                                canPlayback = true;
                                                hideAudioFeedback();
                                            };
                                            
                                            audioPreview.onerror = function() {
                                                canPlayback = false;
                                                if (isIOS) {
                                                    showAudioFeedback("ℹ️ Audio grabado correctamente. En iOS no se puede previsualizar, pero la grabación es válida.", "info");
                                                } else {
                                                    showAudioFeedback("ℹ️ Audio grabado. Si no se escucha, la grabación sigue siendo válida.", "info");
                                                }
                                            };
                                            
                                            // Forzar carga
                                            audioPreview.load();
                                            
                                            // Convert to Base64 con el mimeType correcto
                                            var reader = new FileReader();
                                            reader.readAsDataURL(audioBlob);
                                            reader.onloadend = function() {
                                                hiddenInput.value = reader.result;
                                            }
                                        });

                                        btnRecord.disabled = true;
                                        btnStop.disabled = false;
                                        btnPlay.disabled = true;
                                        btnReset.disabled = true;
                                        status.textContent = "Grabando...";
                                        status.style.color = "red";
                                        
                                    } catch(err) {
                                        console.error(err);
                                        alert("No se pudo acceder al micrófono. Por favor verifique permisos.");
                                    }
                                }

                                function stopRecording() {
                                    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                                        mediaRecorder.stop();
                                        if(stream) stream.getTracks().forEach(track => track.stop());
                                        
                                        btnRecord.disabled = true;
                                        btnStop.disabled = true;
                                        btnPlay.disabled = false;
                                        btnReset.disabled = false;
                                        status.textContent = "Grabación finalizada.";
                                        status.style.color = "green";
                                    }
                                }

                                function playAudio() {
                                    if (audioPreview.src) {
                                        audioPreview.play().catch(function(err) {
                                            showAudioFeedback("ℹ️ No se puede reproducir localmente, pero la grabación es válida.", "info");
                                        });
                                    }
                                }

                                function resetAudio() {
                                    audioBlob = null;
                                    hiddenInput.value = "";
                                    canPlayback = false;
                                    hideAudioFeedback();
                                    btnRecord.disabled = false;
                                    btnStop.disabled = true;
                                    btnPlay.disabled = true;
                                    btnReset.disabled = true;
                                    status.textContent = "Listo para grabar";
                                    status.style.color = "#555";
                                }

                                btnRecord.addEventListener('click', startRecording);
                                btnStop.addEventListener('click', stopRecording);
                                btnPlay.addEventListener('click', playAudio);
                                btnReset.addEventListener('click', resetAudio);
                                
                                // Validación al enviar
                                var form = document.getElementById('acceptForm');
                                form.addEventListener('submit', function(e) {
                                    if (!hiddenInput.value) {
                                        alert("El audio de aceptación es obligatorio. Por favor grabe su aceptación.");
                                        e.preventDefault();
                                    }
                                });
                            })();
                            </script>
                            {% endif %}

                            {% if evento.req_firma %}
                            <div class="signature-container">
                                <label>Firma digital (requerida)</label>
                                <canvas id="signature-pad" class="signature-pad"></canvas>
                                <button type="button" class="btn btn-clear" id="clear-signature">Limpiar firma</button>
                                <div class="file-hint">Máx. {{ MAX_FIRMA_MB }} MB</div>
                                <div id="firma_feedback" class="file-feedback" style="display:none;"></div>
                                <div id="firma_mobile_tip" class="file-feedback info" style="display:none;">
                                    📱 <strong>Uso simple:</strong> Deslize su dedo sobre el recuadro para firmar. Puede limpiar y volver a firmar si es necesario.
                                </div>
                                <input type="hidden" name="firma_base64" id="firma_base64">
                            </div>
                            {% endif %}

                            <div class="checkbox">
                                <label>
                                    <input type="checkbox" name="acepto" required />
                                    Leí y acepto el deslinde de responsabilidad.
                                </label>
                            </div>

                            <button type="submit" class="btn">Aceptar deslinde</button>
                        </form>

                        {% if evento.req_firma %}
                        <script>
                            (function() {
                                // Detección mobile para mostrar tip de firma
                                function isMobile() {
                                    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) || 
                                           (window.innerWidth <= 768);
                                }
                                
                                if (isMobile()) {
                                    var firmaTip = document.getElementById('firma_mobile_tip');
                                    if (firmaTip) firmaTip.style.display = 'block';
                                }
                                
                                var canvas = document.getElementById('signature-pad');
                                var form = document.getElementById('acceptForm');
                                var clearBtn = document.getElementById('clear-signature');
                                var hiddenInput = document.getElementById('firma_base64');
                                
                                // Ajustar canvas al contenedor
                                function resizeCanvas() {
                                    var ratio = Math.max(window.devicePixelRatio || 1, 1);
                                    canvas.width = canvas.offsetWidth * ratio;
                                    canvas.height = canvas.offsetHeight * ratio;
                                    canvas.getContext("2d").scale(ratio, ratio);
                                }
                                window.onresize = resizeCanvas;
                                resizeCanvas();

                                var ctx = canvas.getContext('2d');
                                var drawing = false;
                                var hasSigned = false;

                                function getPos(e) {
                                    var rect = canvas.getBoundingClientRect();
                                    var x, y;
                                    if (e.touches) {
                                        x = e.touches[0].clientX - rect.left;
                                        y = e.touches[0].clientY - rect.top;
                                    } else {
                                        x = e.clientX - rect.left;
                                        y = e.clientY - rect.top;
                                    }
                                    return {x: x, y: y};
                                }

                                function startDraw(e) {
                                    e.preventDefault();
                                    drawing = true;
                                    var pos = getPos(e);
                                    ctx.beginPath();
                                    ctx.moveTo(pos.x, pos.y);
                                }

                                function moveDraw(e) {
                                    if (!drawing) return;
                                    e.preventDefault();
                                    var pos = getPos(e);
                                    ctx.lineTo(pos.x, pos.y);
                                    ctx.stroke();
                                    hasSigned = true;
                                }

                                function endDraw(e) {
                                    drawing = false;
                                }

                                canvas.addEventListener('mousedown', startDraw);
                                canvas.addEventListener('mousemove', moveDraw);
                                canvas.addEventListener('mouseup', endDraw);
                                canvas.addEventListener('mouseout', endDraw);

                                canvas.addEventListener('touchstart', startDraw);
                                canvas.addEventListener('touchmove', moveDraw);
                                canvas.addEventListener('touchend', endDraw);

                                clearBtn.addEventListener('click', function() {
                                    ctx.clearRect(0, 0, canvas.width, canvas.height);
                                    hasSigned = false;
                                    hiddenInput.value = "";
                                });

                                form.addEventListener('submit', function(e) {
                                    if (hasSigned) {
                                        var dataUrl = canvas.toDataURL("image/png");
                                        // Estimar tamaño aproximado (base64 es ~33% más grande que binario)
                                        var base64Size = dataUrl.length;
                                        var estimatedSize = (base64Size * 3) / 4;
                                        var maxFirmaBytes = {{ MAX_FIRMA_MB }} * 1024 * 1024;
                                        
                                        if (estimatedSize > maxFirmaBytes) {
                                            var feedback = document.getElementById('firma_feedback');
                                            if (feedback) {
                                                feedback.textContent = "⚠️ La firma es demasiado grande. Por favor, firme más pequeña.";
                                                feedback.className = 'file-feedback error';
                                                feedback.style.display = 'block';
                                            }
                                            e.preventDefault();
                                            return;
                                        }
                                        
                                        hiddenInput.value = dataUrl;
                                    } else {
                                        // Si es obligatorio, impedir submit
                                        alert("Por favor, firme en el recuadro.");
                                        e.preventDefault();
                                    }
                                });
                            })();
                        </script>
                        {% endif %}
                    {% endif %}
                </div>
            </body>
            </html>
            """,
            # Plantilla de detalle de aceptación
            "admin_aceptacion_detalle.html": """
            <!doctype html>
            <html lang="es">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Detalle Aceptación #{{ aceptacion.id }}</title>
                <style>
                    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
                    .card { max-width: 800px; margin: 0 auto; padding: 24px; border: 1px solid #ddd; border-radius: 8px; }
                    .field { margin-bottom: 16px; }
                    .label { font-weight: bold; display: block; color: #555; }
                    .value { word-break: break-all; }
                    .status-ok { color: green; font-weight: bold; }
                    .status-missing { color: red; font-weight: bold; }
                    h2 { margin-top: 24px; border-bottom: 1px solid #eee; padding-bottom: 8px; }
                    .btn-back { display: inline-block; margin-bottom: 16px; text-decoration: none; color: #0d6efd; }
                </style>
            </head>
            <body>
                <div class="card">
                    <a href="/admin/aceptaciones" class="btn-back">← Volver a lista</a>
                    <h1>Aceptación #{{ aceptacion.id }}</h1>
                    
                    <h2>Participante</h2>
                    <div class="field">
                        <span class="label">Nombre:</span>
                        <span class="value">{{ aceptacion.nombre_participante }}</span>
                    </div>
                    <div class="field">
                        <span class="label">Documento:</span>
                        <span class="value">{{ aceptacion.documento }}</span>
                    </div>

                    <h2>Evento</h2>
                    <div class="field">
                        <span class="label">Evento:</span>
                        <span class="value">{{ aceptacion.evento_nombre }} ({{ aceptacion.evento_fecha }})</span>
                    </div>
                    <div class="field">
                        <span class="label">Organizador:</span>
                        <span class="value">{{ aceptacion.evento_organizador }}</span>
                    </div>

                    <h2>Auditoría</h2>
                    <div class="field">
                        <span class="label">Fecha/Hora (UTC):</span>
                        <span class="value">{{ aceptacion.fecha_hora }}</span>
                    </div>
                    <div class="field">
                        <span class="label">IP:</span>
                        <span class="value">{{ aceptacion.ip }}</span>
                    </div>
                    <div class="field">
                        <span class="label">User Agent:</span>
                        <span class="value">{{ aceptacion.user_agent }}</span>
                    </div>
                    <div class="field">
                        <span class="label">Hash Deslinde:</span>
                        <span class="value">{{ aceptacion.deslinde_hash_sha256 }}</span>
                    </div>

                    <h2>Evidencias</h2>
                    
                    <div class="field">
                        <span class="label">Firma:</span>
                        <div class="value">Path: {{ aceptacion.firma_path or 'N/A' }}</div>
                        <div>Estado: 
                            {% if aceptacion.firma_path %}
                                <span class="{{ 'status-ok' if aceptacion.firma_exists else 'status-missing' }}">
                                    {{ 'ARCHIVO EXISTE' if aceptacion.firma_exists else 'ARCHIVO NO ENCONTRADO' }}
                                </span>
                            {% else %}
                                -
                            {% endif %}
                        </div>
                    </div>

                    <div class="field">
                        <span class="label">Documento Frente:</span>
                        <div class="value">Path: {{ aceptacion.doc_frente_path or 'N/A' }}</div>
                        <div>Estado: 
                            {% if aceptacion.doc_frente_path %}
                                <span class="{{ 'status-ok' if aceptacion.doc_frente_exists else 'status-missing' }}">
                                    {{ 'ARCHIVO EXISTE' if aceptacion.doc_frente_exists else 'ARCHIVO NO ENCONTRADO' }}
                                </span>
                            {% else %}
                                -
                            {% endif %}
                        </div>
                    </div>

                    <div class="field">
                        <span class="label">Documento Dorso:</span>
                        <div class="value">Path: {{ aceptacion.doc_dorso_path or 'N/A' }}</div>
                        <div>Estado: 
                            {% if aceptacion.doc_dorso_path %}
                                <span class="{{ 'status-ok' if aceptacion.doc_dorso_exists else 'status-missing' }}">
                                    {{ 'ARCHIVO EXISTE' if aceptacion.doc_dorso_exists else 'ARCHIVO NO ENCONTRADO' }}
                                </span>
                            {% else %}
                                -
                            {% endif %}
                        </div>
                    </div>

                    <div class="field">
                        <span class="label">Audio:</span>
                        <div class="value">Path: {{ aceptacion.audio_path or 'N/A' }}</div>
                        <div>Estado: 
                            {% if aceptacion.audio_path %}
                                <span class="{{ 'status-ok' if aceptacion.audio_exists else 'status-missing' }}">
                                    {{ 'ARCHIVO EXISTE' if aceptacion.audio_exists else 'ARCHIVO NO ENCONTRADO' }}
                                </span>
                            {% else %}
                                -
                            {% endif %}
                        </div>
                    </div>

                </div>
            </body>
            </html>
            """,            # Plantilla de confirmación
            "confirmacion.html": """
            <!doctype html>
            <html lang="es">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Deslinde aceptado</title>
                <style>
                    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
                    .card { max-width: 640px; margin: 0 auto; padding: 24px; border: 1px solid #ddd; border-radius: 8px; }
                    .muted { color: #666; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>Deslinde aceptado</h1>
                    <p>Gracias {{ nombre_participante }}. Tu aceptación quedó registrada para el evento <strong>{{ evento.nombre }}</strong>.</p>
                    <p class="muted">Registro ID: {{ aceptacion_id }} — {{ fecha_hora }}</p>
                </div>
            </body>
            </html>
            """,
            # Plantilla de listado admin (sin auth en el MVP)
            "admin_aceptaciones.html": """
            <!doctype html>
            <html lang="es">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Admin - Aceptaciones</title>
                <style>
                    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
                    table { border-collapse: collapse; width: 100%; }
                    th, td { border: 1px solid #ddd; padding: 8px; }
                    th { background: #f2f2f2; text-align: left; }
                    .muted { color: #666; font-size: 0.95em; }
                </style>
            </head>
            <body>
                <h1>Aceptaciones</h1>
                <p class="muted">Listado básico sin autenticación (MVP).</p>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Evento</th>
                            <th>Fecha evento</th>
                            <th>Organizador</th>
                            <th>Nombre participante</th>
                            <th>Documento</th>
                            <th>Fecha/Hora</th>
                            <th>IP</th>
                            <th>User Agent</th>
                            <th>Firma Path</th>
                            <th>Doc Frente Path</th>
                            <th>Doc Dorso Path</th>
                            <th>Audio Path</th>
                        </tr>
                    </thead>
                    <tbody>
                    {% for a in aceptaciones %}
                        <tr>
                            <td><a href="/admin/aceptaciones/{{ a.id }}">{{ a.id }}</a></td>
                            <td>{{ a.evento_nombre }}</td>
                            <td>{{ a.evento_fecha|fecha_ddmmaaaa }}</td>
                            <td>{{ a.evento_organizador }}</td>
                            <td>{{ a.nombre_participante }}</td>
                            <td>{{ a.documento }}</td>
                            <td>{{ a.fecha_hora }}</td>
                            <td>{{ a.ip }}</td>
                            <td>{{ a.user_agent }}</td>
                            <td style="font-size: 0.85em; max-width: 200px; overflow: hidden; text-overflow: ellipsis;">{{ a.firma_path or '-' }}</td>
                            <td style="font-size: 0.85em; max-width: 200px; overflow: hidden; text-overflow: ellipsis;">{{ a.doc_frente_path or '-' }}</td>
                            <td style="font-size: 0.85em; max-width: 200px; overflow: hidden; text-overflow: ellipsis;">{{ a.doc_dorso_path or '-' }}</td>
                            <td style="font-size: 0.85em; max-width: 200px; overflow: hidden; text-overflow: ellipsis;">{{ a.audio_path or '-' }}</td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            </body>
            </html>
            """,
        }
    ),
    autoescape=select_autoescape(["html", "xml"]),
)

def fecha_ddmmaaaa(value: str) -> str:
    try:
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return value
templates_env.filters["fecha_ddmmaaaa"] = fecha_ddmmaaaa

# ------------------------------------------------------------------------------
# Configuración de base de datos SQLite y Almacenamiento
# ------------------------------------------------------------------------------
DEFAULT_DB_PATH = "/var/lib/encarreraok/encarreraok.sqlite3"
DB_PATH = os.environ.get("ENCARRERAOK_DB_PATH", DEFAULT_DB_PATH)
EVIDENCIAS_DIR = os.path.join(os.path.dirname(DB_PATH), "evidencias")
FIRMAS_DIR = os.path.join(EVIDENCIAS_DIR, "firmas")
DOCUMENTOS_DIR = os.path.join(EVIDENCIAS_DIR, "documentos")
AUDIOS_DIR = os.path.join(EVIDENCIAS_DIR, "audios")


def ensure_storage() -> None:
    """
    Garantiza que directorios de DB y evidencias existan con permisos.
    """
    # Directorio base y DB
    db_dir = os.path.dirname(DB_PATH)
    try:
        os.makedirs(db_dir, exist_ok=True)
        # Permisos 0750 (rwxr-x---) para directorio base
        try:
            os.chmod(db_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        except Exception:
            pass
            
        if os.path.exists(DB_PATH):
            try:
                os.chmod(DB_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            except Exception:
                pass
                
        # Directorios de evidencias
        os.makedirs(FIRMAS_DIR, exist_ok=True)
        os.makedirs(DOCUMENTOS_DIR, exist_ok=True)
        os.makedirs(AUDIOS_DIR, exist_ok=True)
        # Podríamos ajustar permisos de evidencias también
    except Exception:
        # Entorno local dev windows etc
        pass


def get_connection() -> sqlite3.Connection:
    """
    Crea una conexión a la base SQLite.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Inicializa la base de datos y aplica migraciones manuales si es necesario.
    """
    ensure_storage()
    conn = get_connection()
    try:
        cur = conn.cursor()
        # Tabla de eventos
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY,
                nombre TEXT NOT NULL,
                fecha TEXT NOT NULL,          -- ISO: YYYY-MM-DD
                organizador TEXT NOT NULL,
                activo INTEGER NOT NULL CHECK (activo IN (0,1)),
                req_firma INTEGER DEFAULT 0 CHECK (req_firma IN (0,1)),
                req_documento INTEGER DEFAULT 0 CHECK (req_documento IN (0,1)),
                req_audio INTEGER DEFAULT 0 CHECK (req_audio IN (0,1))
            )
            """
        )
        
        # Migración: req_firma en eventos
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_firma INTEGER DEFAULT 0 CHECK (req_firma IN (0,1))")
        except sqlite3.OperationalError:
            pass
            
        # Migración: req_documento en eventos
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_documento INTEGER DEFAULT 0 CHECK (req_documento IN (0,1))")
        except sqlite3.OperationalError:
            pass

        # Migración: req_audio en eventos
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_audio INTEGER DEFAULT 0 CHECK (req_audio IN (0,1))")
        except sqlite3.OperationalError:
            pass

        # Tabla de aceptaciones
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS aceptaciones (
                id INTEGER PRIMARY KEY,
                evento_id INTEGER NOT NULL,
                nombre_participante TEXT NOT NULL,
                documento TEXT NOT NULL,
                fecha_hora TEXT NOT NULL,     -- ISO: YYYY-MM-DDTHH:MM:SSZ (sin zona)
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                deslinde_hash_sha256 TEXT,
                firma_path TEXT,
                doc_frente_path TEXT,
                doc_dorso_path TEXT,
                audio_path TEXT,
                FOREIGN KEY (evento_id) REFERENCES eventos(id)
            )
            """
        )
        
        # Migración: firma_path en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN firma_path TEXT")
        except sqlite3.OperationalError:
            pass
            
        # Migración: doc_frente_path y doc_dorso_path en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN doc_frente_path TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN doc_dorso_path TEXT")
        except sqlite3.OperationalError:
            pass
            
        # Migración: audio_path en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN audio_path TEXT")
        except sqlite3.OperationalError:
            pass
            
        # Tabla de deslindes versionados
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deslindes (
                id INTEGER PRIMARY KEY,
                evento_id INTEGER NOT NULL,
                texto TEXT NOT NULL,
                hash_sha256 TEXT NOT NULL,
                activo INTEGER NOT NULL CHECK (activo IN (0,1)),
                fecha_creacion TEXT,          -- ISO UTC
                creado_por TEXT,
                FOREIGN KEY (evento_id) REFERENCES eventos(id)
            )
            """
        )
        
        # Migración manual simple: intentar agregar columnas si no existen
        try:
            cur.execute("ALTER TABLE deslindes ADD COLUMN fecha_creacion TEXT")
        except sqlite3.OperationalError:
            pass # Ya existe
            
        try:
            cur.execute("ALTER TABLE deslindes ADD COLUMN creado_por TEXT")
        except sqlite3.OperationalError:
            pass # Ya existe

        # Índice único parcial: un solo deslinde activo por evento
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_deslindes_evento_activo
            ON deslindes(evento_id) WHERE activo = 1
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_evento(evento_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene un evento por id."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insertar_aceptacion(
    evento_id: int,
    nombre_participante: str,
    documento: str,
    fecha_hora: str,
    ip: str,
    user_agent: str,
    deslinde_hash_sha256: str,
    firma_path: Optional[str] = None,
    doc_frente_path: Optional[str] = None,
    doc_dorso_path: Optional[str] = None,
    audio_path: Optional[str] = None,
) -> int:
    """Inserta una aceptación y devuelve el ID creado."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO aceptaciones (
                evento_id, nombre_participante, documento, fecha_hora, ip, user_agent, deslinde_hash_sha256, firma_path, doc_frente_path, doc_dorso_path, audio_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evento_id, nombre_participante, documento, fecha_hora, ip, user_agent, deslinde_hash_sha256, firma_path, doc_frente_path, doc_dorso_path, audio_path),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def listar_aceptaciones() -> List[Dict[str, Any]]:
    """Lista aceptaciones con datos del evento (join simple)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id,
                a.evento_id,
                e.nombre AS evento_nombre,
                e.fecha AS evento_fecha,
                e.organizador AS evento_organizador,
                a.nombre_participante,
                a.documento,
                a.fecha_hora,
                a.ip,
                a.user_agent,
                a.deslinde_hash_sha256,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            ORDER BY a.id DESC
            """
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_aceptacion_detalle(aceptacion_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene detalle completo de una aceptación con verificación de existencia de archivos."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id,
                a.evento_id,
                e.nombre AS evento_nombre,
                e.fecha AS evento_fecha,
                e.organizador AS evento_organizador,
                a.nombre_participante,
                a.documento,
                a.fecha_hora,
                a.ip,
                a.user_agent,
                a.deslinde_hash_sha256,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE a.id = ?
            """,
            (aceptacion_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        
        data = dict(row)
        
        # Verificar existencia de archivos
        data['firma_exists'] = os.path.exists(data['firma_path']) if data['firma_path'] else False
        data['doc_frente_exists'] = os.path.exists(data['doc_frente_path']) if data['doc_frente_path'] else False
        data['doc_dorso_exists'] = os.path.exists(data['doc_dorso_path']) if data['doc_dorso_path'] else False
        data['audio_exists'] = os.path.exists(data['audio_path']) if data['audio_path'] else False
        
        return data
    finally:
        conn.close()


def calcular_hash_sha256(texto: str) -> str:
    """Calcula SHA256 en hex del texto provisto."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def comprimir_imagen(file_path: str, max_size_mb: float = MAX_IMAGE_COMPRESS_TARGET_MB) -> Optional[str]:
    """
    Comprime una imagen si es posible usando PIL.
    Retorna la ruta del archivo comprimido o None si no se pudo comprimir.
    Si PIL no está disponible, retorna None.
    """
    if not PIL_AVAILABLE:
        return None
    
    try:
        max_size_bytes = int(max_size_mb * 1024 * 1024)
        
        # Abrir imagen
        img = Image.open(file_path)
        original_format = img.format or 'JPEG'
        
        # Convertir a RGB si es necesario (para JPEG)
        if original_format in ('JPEG', 'JPG') and img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Calcular tamaño actual
        buffer = io.BytesIO()
        img.save(buffer, format=original_format, quality=85, optimize=True)
        current_size = buffer.tell()
        
        if current_size <= max_size_bytes:
            # Ya está dentro del límite
            return file_path
        
        # Reducir resolución manteniendo aspecto
        original_width, original_height = img.size
        ratio = (max_size_bytes / current_size) ** 0.5  # Factor de reducción
        new_width = int(original_width * ratio)
        new_height = int(original_height * ratio)
        
        # Asegurar mínimo de 800px en el lado más largo
        if max(new_width, new_height) < 800:
            if new_width > new_height:
                new_width = 800
                new_height = int(original_height * (800 / original_width))
            else:
                new_height = 800
                new_width = int(original_width * (800 / original_height))
        
        # Redimensionar (compatible con versiones antiguas de PIL)
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        img_resized = img.resize((new_width, new_height), resample)
        
        # Intentar diferentes calidades hasta alcanzar el tamaño objetivo
        for quality in [85, 75, 65, 55, 45]:
            buffer = io.BytesIO()
            img_resized.save(buffer, format=original_format, quality=quality, optimize=True)
            if buffer.tell() <= max_size_bytes:
                # Guardar archivo comprimido
                with open(file_path, 'wb') as f:
                    f.write(buffer.getvalue())
                return file_path
        
        # Si aún no cumple, usar calidad mínima
        buffer = io.BytesIO()
        img_resized.save(buffer, format=original_format, quality=40, optimize=True)
        if buffer.tell() <= max_size_bytes * 1.2:  # Tolerancia del 20%
            with open(file_path, 'wb') as f:
                f.write(buffer.getvalue())
            return file_path
        
        return None
    except Exception:
        return None


def get_deslinde_activo(evento_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene el deslinde activo para un evento."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, evento_id, texto, hash_sha256, activo
            FROM deslindes
            WHERE evento_id = ? AND activo = 1
            LIMIT 1
            """,
            (evento_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insertar_deslinde(
    evento_id: int,
    texto: str,
    activo: int = 1,
    creado_por: str = "sistema",
) -> int:
    """Inserta un deslinde para un evento (por defecto activo) y devuelve su ID."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        hashv = calcular_hash_sha256(texto)
        fecha_creacion = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        cur.execute(
            """
            INSERT INTO deslindes (evento_id, texto, hash_sha256, activo, fecha_creacion, creado_por)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (evento_id, texto, hashv, activo, fecha_creacion, creado_por),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Modelos de datos (Pydantic) para documentación y validación básica
# ------------------------------------------------------------------------------
class Evento(BaseModel):
    id: int
    nombre: str
    fecha: date
    organizador: str
    activo: bool
    req_firma: bool = False
    req_documento: bool = False
    req_audio: bool = False


class Aceptacion(BaseModel):
    id: int
    evento_id: int
    nombre_participante: str
    documento: str
    fecha_hora: datetime
    ip: str
    user_agent: str
    firma_path: Optional[str] = None
    doc_frente_path: Optional[str] = None
    doc_dorso_path: Optional[str] = None
    audio_path: Optional[str] = None


# ------------------------------------------------------------------------------
# Hooks de arranque: inicializa base y crea un evento de ejemplo si vacío
# ------------------------------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    """
    Inicializa la base y, si no hay eventos, crea uno de ejemplo para pruebas.
    """
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM eventos")
        count = cur.fetchone()["c"]
        if count == 0:
            cur.execute(
                """
                INSERT INTO eventos (id, nombre, fecha, organizador, activo)
                VALUES (?, ?, ?, ?, ?)
                """,
                (1, "Carrera 10K Montevideo", date.today().isoformat(), "Encarrera", 1),
            )
            conn.commit()
        
        # Asegura que cada evento tenga exactamente un deslinde activo
        cur.execute("SELECT id, nombre, organizador FROM eventos")
        eventos = [dict(r) for r in cur.fetchall()]
        
        for evt in eventos:
            eid = evt["id"]
            cur.execute("SELECT COUNT(*) AS c FROM deslindes WHERE evento_id = ? AND activo = 1", (eid,))
            has_active = cur.fetchone()["c"] > 0
            if not has_active:
                # Reemplazar placeholders en texto base
                texto_final = DESLINDE_TEXTO_BASE.replace("{{NOMBRE_EVENTO}}", evt["nombre"])\
                                                 .replace("{{ORGANIZADOR}}", evt["organizador"])
                
                insertar_deslinde(
                    evento_id=eid,
                    texto=texto_final,
                    activo=1,
                    creado_por="sistema"
                )
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Endpoints del MVP
# ------------------------------------------------------------------------------
@app.get("/e/{evento_id}", response_class=HTMLResponse)
def mostrar_formulario(evento_id: int, request: Request) -> HTMLResponse:
    """
    Muestra el formulario de aceptación para un evento.
    - Si el evento no existe, retorna 404.
    - Si el evento está inactivo, muestra el formulario deshabilitado.
    - Requiere un deslinde activo para el evento.
    """
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    # Normaliza booleano 'activo' (0/1 en SQLite)
    evento["activo"] = bool(evento["activo"])
    evento["req_firma"] = bool(evento.get("req_firma", 0))
    evento["req_documento"] = bool(evento.get("req_documento", 0))
    evento["req_audio"] = bool(evento.get("req_audio", 0))
    deslinde = get_deslinde_activo(evento_id)
    if not deslinde:
        raise HTTPException(status_code=400, detail="No existe deslinde activo para el evento")
    template = templates_env.get_template("evento_form.html")
    html = template.render(
        evento=evento, 
        request=request, 
        deslinde_texto=deslinde["texto"],
        MAX_IMAGE_DOC_MB=MAX_IMAGE_DOC_MB,
        MAX_FIRMA_MB=MAX_FIRMA_MB,
        MAX_AUDIO_MB=MAX_AUDIO_MB,
        MAX_IMAGE_COMPRESS_THRESHOLD_MB=MAX_IMAGE_COMPRESS_THRESHOLD_MB
    )
    return HTMLResponse(content=html)


@app.post("/e/{evento_id}", response_class=HTMLResponse)
def procesar_aceptacion(
    evento_id: int,
    request: Request,
    nombre_participante: str = Form(...),
    documento: str = Form(...),
    acepto: Optional[str] = Form(None),
    firma_base64: Optional[str] = Form(None),
    doc_frente: Optional[UploadFile] = File(None),
    doc_dorso: Optional[UploadFile] = File(None),
    audio_base64: Optional[str] = Form(None),
) -> HTMLResponse:
    """
    Procesa el formulario de aceptación:
    - Verifica existencia y estado del evento
    - Requiere checkbox 'acepto' marcado
    - Guarda registro en SQLite con IP y User-Agent
    - Normaliza documento
    - Usa fecha/hora UTC con sufijo 'Z'
    - Asocia el hash del deslinde activo aceptado
    - Guarda firma manuscrita si el evento lo requiere
    - Guarda imágenes de documento si el evento lo requiere
    - Guarda audio de aceptación si el evento lo requiere
    - Renderiza confirmación
    """
    # Generar request_id único para trazabilidad
    request_id = str(uuid.uuid4())[:8]
    
    try:
        app_logger.info(f"[{request_id}] Inicio procesamiento aceptación - evento_id={evento_id}")
        
        evento = get_evento(evento_id)
        if not evento:
            app_logger.warning(f"[{request_id}] Evento no encontrado: evento_id={evento_id}")
            raise HTTPException(status_code=404, detail="Evento no encontrado")
        if not bool(evento["activo"]):
            app_logger.warning(f"[{request_id}] Evento inactivo: evento_id={evento_id}")
            raise HTTPException(status_code=400, detail="Evento inactivo")
        # Validación del checkbox (HTML ya tiene required, pero validamos servidor)
        if acepto is None:
            app_logger.warning(f"[{request_id}] Checkbox acepto no marcado")
            raise HTTPException(status_code=400, detail="Debe aceptar el deslinde")

        # Validación de firma
        req_firma = bool(evento.get("req_firma", 0))
        if req_firma and not firma_base64:
             raise HTTPException(status_code=400, detail="La firma manuscrita es obligatoria")
             
        # Validación de documento
        req_documento = bool(evento.get("req_documento", 0))
        if req_documento:
            if not doc_frente or not doc_frente.filename:
                raise HTTPException(status_code=400, detail="La foto del frente del documento es obligatoria")
            if not doc_dorso or not doc_dorso.filename:
                raise HTTPException(status_code=400, detail="La foto del dorso del documento es obligatoria")
            
            # Validación de tamaño backend (defensiva) - esta validación se hace después en procesamiento
            # pero mantenemos aquí como validación temprana
            try:
                doc_frente.file.seek(0, os.SEEK_END)
                size_frente = doc_frente.file.tell()
                doc_frente.file.seek(0)
                
                doc_dorso.file.seek(0, os.SEEK_END)
                size_dorso = doc_dorso.file.tell()
                doc_dorso.file.seek(0)
                
                max_bytes_img = MAX_IMAGE_DOC_MB * 1024 * 1024
                if size_frente > max_bytes_img or size_dorso > max_bytes_img:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Las imágenes no deben superar {MAX_IMAGE_DOC_MB} MB cada una."
                    )
            except HTTPException:
                raise
            except Exception:
                # Si falla la verificación de tamaño, continuamos (validación más estricta después)
                pass

        # Validación de audio
        req_audio = bool(evento.get("req_audio", 0))
        if req_audio and not audio_base64:
            raise HTTPException(status_code=400, detail="El audio de aceptación es obligatorio")

        # Metadatos del cliente
        ip = request.client.host if request.client else "0.0.0.0"
        user_agent = request.headers.get("user-agent", "")
        fecha_hora = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        # Normalización de documento: quitar puntos, guiones y espacios; a mayúsculas
        documento_norm = re.sub(r"[.\-\s]", "", documento).upper()
        # Obtiene deslinde activo y su hash
        deslinde = get_deslinde_activo(evento_id)
        if not deslinde:
            raise HTTPException(status_code=400, detail="No existe deslinde activo para el evento")

        # Procesamiento de firma
        firma_path_final = None
        if firma_base64:
            # data:image/png;base64,.....
            # Separar encabezado si existe
            if "," in firma_base64:
                header, encoded = firma_base64.split(",", 1)
            else:
                encoded = firma_base64
            
            try:
                data = base64.b64decode(encoded)
                
                # Validación tamaño firma (prevención 413)
                firma_size = len(data)
                max_firma_bytes = MAX_FIRMA_MB * 1024 * 1024
                if firma_size > max_firma_bytes:
                    app_logger.warning(f"[{request_id}] Firma demasiado grande: {firma_size} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La firma es demasiado grande. Máximo permitido: {MAX_FIRMA_MB} MB. Por favor, firme más pequeña."
                    )
                
                filename = f"{uuid.uuid4()}.png"
                filepath = os.path.join(FIRMAS_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(data)
                firma_path_final = filepath
                app_logger.info(f"[{request_id}] Firma guardada: path={filepath}, size={firma_size} bytes")
            except HTTPException:
                raise
            except Exception:
                # Si falla guardar la firma y es requerida, error.
                if req_firma:
                    raise HTTPException(status_code=500, detail="Error al guardar la firma")
                # Si no es requerida pero vino data corrupta, se ignora o se loguea.
            
        # Procesamiento de documentos
        doc_frente_path_final = None
        doc_dorso_path_final = None
        
        if req_documento and doc_frente and doc_dorso:
            try:
                # Validación tamaño documentos (prevención 413) - ANTES de guardar
                max_doc_bytes = MAX_IMAGE_DOC_MB * 1024 * 1024
                
                # Validar frente
                doc_frente.file.seek(0, os.SEEK_END)
                size_frente = doc_frente.file.tell()
                doc_frente.file.seek(0)
                if size_frente > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc frente demasiado grande: {size_frente} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La imagen del frente es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )
            
                # Validar dorso
                doc_dorso.file.seek(0, os.SEEK_END)
                size_dorso = doc_dorso.file.tell()
                doc_dorso.file.seek(0)
                if size_dorso > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc dorso demasiado grande: {size_dorso} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La imagen del dorso es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )
            
                # Frente
                ext_frente = os.path.splitext(doc_frente.filename)[1]
                if not ext_frente: ext_frente = ".jpg"
                filename_frente = f"{uuid.uuid4()}_frente{ext_frente}"
                filepath_frente = os.path.join(DOCUMENTOS_DIR, filename_frente)
                with open(filepath_frente, "wb") as buffer:
                    shutil.copyfileobj(doc_frente.file, buffer)
            
                # Comprimir si es necesario (si supera 2MB)
                if size_frente > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc frente: {size_frente} bytes")
                    compressed = comprimir_imagen(filepath_frente, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
                        # Si no se pudo comprimir, rechazar
                        os.remove(filepath_frente)
                        app_logger.error(f"[{request_id}] No se pudo comprimir doc frente")
                        raise HTTPException(
                            status_code=413,
                            detail=f"La imagen del frente es demasiado grande y no se pudo comprimir. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                        )
                    final_size_frente = os.path.getsize(filepath_frente)
                    app_logger.info(f"[{request_id}] Doc frente comprimido: {size_frente} -> {final_size_frente} bytes")
                else:
                    final_size_frente = size_frente
            
                doc_frente_path_final = filepath_frente
                app_logger.info(f"[{request_id}] Doc frente guardado: path={filepath_frente}, size={final_size_frente} bytes")
            
                # Dorso
                ext_dorso = os.path.splitext(doc_dorso.filename)[1]
                if not ext_dorso: ext_dorso = ".jpg"
                filename_dorso = f"{uuid.uuid4()}_dorso{ext_dorso}"
                filepath_dorso = os.path.join(DOCUMENTOS_DIR, filename_dorso)
                with open(filepath_dorso, "wb") as buffer:
                    shutil.copyfileobj(doc_dorso.file, buffer)
            
                # Comprimir si es necesario
                if size_dorso > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc dorso: {size_dorso} bytes")
                    compressed = comprimir_imagen(filepath_dorso, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
                        # Si no se pudo comprimir, rechazar
                        os.remove(filepath_dorso)
                        if doc_frente_path_final and os.path.exists(doc_frente_path_final):
                            os.remove(doc_frente_path_final)
                        app_logger.error(f"[{request_id}] No se pudo comprimir doc dorso")
                        raise HTTPException(
                            status_code=413,
                            detail=f"La imagen del dorso es demasiado grande y no se pudo comprimir. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                        )
                    final_size_dorso = os.path.getsize(filepath_dorso)
                    app_logger.info(f"[{request_id}] Doc dorso comprimido: {size_dorso} -> {final_size_dorso} bytes")
                else:
                    final_size_dorso = size_dorso
            
                doc_dorso_path_final = filepath_dorso
                app_logger.info(f"[{request_id}] Doc dorso guardado: path={filepath_dorso}, size={final_size_dorso} bytes")
                
            except HTTPException:
                raise
            except Exception as e:
                # Limpiar archivos parciales en caso de error
                if doc_frente_path_final and os.path.exists(doc_frente_path_final):
                    try:
                        os.remove(doc_frente_path_final)
                    except:
                        pass
                if doc_dorso_path_final and os.path.exists(doc_dorso_path_final):
                    try:
                        os.remove(doc_dorso_path_final)
                    except:
                        pass
                raise HTTPException(status_code=500, detail="Error al guardar las imágenes del documento")

        # Procesamiento de audio
        audio_path_final = None
        if audio_base64:
            # data:audio/webm;base64,.....
            header = ""
            if "," in audio_base64:
                header, encoded = audio_base64.split(",", 1)
            else:
                encoded = audio_base64
            
            try:
                data = base64.b64decode(encoded)
                
                # Validación tamaño audio backend (prevención 413)
                max_audio_bytes = MAX_AUDIO_MB * 1024 * 1024
                audio_size = len(data)
                if audio_size > max_audio_bytes:
                    app_logger.warning(f"[{request_id}] Audio demasiado grande: {audio_size} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"El audio es demasiado grande. Máximo permitido: {MAX_AUDIO_MB} MB. Por favor, intente ser más breve."
                    )
                
                # Extensión default
                ext = ".webm"
                if "audio/mp3" in header: ext = ".mp3"
                elif "audio/wav" in header: ext = ".wav"
                elif "audio/ogg" in header: ext = ".ogg"
                elif "audio/mp4" in header: ext = ".mp4"
                
                filename_audio = f"{uuid.uuid4()}{ext}"
                filepath_audio = os.path.join(AUDIOS_DIR, filename_audio)
                with open(filepath_audio, "wb") as f:
                    f.write(data)
                audio_path_final = filepath_audio
                app_logger.info(f"[{request_id}] Audio guardado: path={filepath_audio}, size={audio_size} bytes")
            except HTTPException:
                raise
            except Exception:
                if req_audio:
                    raise HTTPException(status_code=500, detail="Error al guardar el audio")

        aceptacion_id = insertar_aceptacion(
            evento_id=evento_id,
            nombre_participante=nombre_participante.strip(),
            documento=documento_norm,
            fecha_hora=fecha_hora,
            ip=ip,
            user_agent=user_agent,
            deslinde_hash_sha256=deslinde["hash_sha256"],
            firma_path=firma_path_final,
            doc_frente_path=doc_frente_path_final,
            doc_dorso_path=doc_dorso_path_final,
            audio_path=audio_path_final,
        )
        
        # Log final con todos los datos
        app_logger.info(
            f"[{request_id}] Aceptación guardada exitosamente - "
            f"aceptacion_id={aceptacion_id}, evento_id={evento_id}, "
            f"firma_path={firma_path_final}, doc_frente_path={doc_frente_path_final}, "
            f"doc_dorso_path={doc_dorso_path_final}, audio_path={audio_path_final}"
        )

        template = templates_env.get_template("confirmacion.html")
        html = template.render(
            nombre_participante=nombre_participante,
            evento=evento,
            aceptacion_id=aceptacion_id,
            fecha_hora=fecha_hora,
        )
        return HTMLResponse(content=html)
    except HTTPException:
        raise
    except Exception as e:
        # Loggear cualquier excepción con stacktrace
        app_logger.error(
            f"[{request_id}] Excepción en procesar_aceptacion - evento_id={evento_id}: {str(e)}\n"
            f"{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail="Error interno del servidor")


@app.get("/admin/aceptaciones", response_class=HTMLResponse)
def admin_aceptaciones() -> HTMLResponse:
    """
    Lista de aceptaciones.
    - Sin autenticación en el MVP.
    - Ordenadas por ID descendente.
    """
    datos = listar_aceptaciones()
    template = templates_env.get_template("admin_aceptaciones.html")
    html = template.render(aceptaciones=datos)
    return HTMLResponse(content=html)


@app.get("/admin/aceptaciones/{aceptacion_id}", response_class=HTMLResponse)
def admin_aceptacion_detalle(aceptacion_id: int) -> HTMLResponse:
    """
    Muestra detalle de una aceptación específica.
    - Incluye todos los datos + paths + verificación de existencia de archivos.
    """
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
    
    template = templates_env.get_template("admin_aceptacion_detalle.html")
    html = template.render(aceptacion=aceptacion)
    return HTMLResponse(content=html)


# ------------------------------------------------------------------------------
# Ejecutable local (opcional). En producción se usa systemd + uvicorn.
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    # Servidor local para pruebas:
    #   python main.py
    #   Navegar a: http://127.0.0.1:8000/docs
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# ==============================================================================
# PLAN DE PRUEBAS MANUALES
# ==============================================================================
#
# Objetivo: Verificar logging, visualización de paths y detalle de aceptaciones
#
# PREREQUISITOS:
# - Servidor corriendo (python main.py o systemd)
# - Directorio /var/log/encarreraok debe existir o tener permisos
# - Evento creado (se crea automáticamente si la DB está vacía)
#
# PASO 1: Verificar logging básico
#   - Acceder a GET /e/1 (formulario)
#   - Verificar que /var/log/encarreraok/app.log existe
#   - Comando: tail -f /var/log/encarreraok/app.log
#   - Esperar: No debería haber logs aún (solo POST genera logs)
#
# PASO 2: Crear aceptación con todas las evidencias (Desktop)
#   - Navegar a http://localhost:8000/e/1
#   - Completar formulario:
#     * Nombre: "Test Usuario"
#     * Documento: "12345678"
#     * Subir foto frente documento (imagen < 4MB)
#     * Subir foto dorso documento (imagen < 4MB)
#     * Grabar audio de aceptación (< 5MB)
#     * Firmar en canvas
#     * Marcar checkbox acepto
#   - Enviar formulario
#   - Verificar logs esperados:
#     * [request_id] Inicio procesamiento aceptación - evento_id=1
#     * [request_id] Firma guardada: path=..., size=... bytes
#     * [request_id] Doc frente guardado: path=..., size=... bytes
#     * [request_id] Doc dorso guardado: path=..., size=... bytes
#     * [request_id] Audio guardado: path=..., size=... bytes
#     * [request_id] Aceptación guardada exitosamente - aceptacion_id=...
#
# PASO 3: Verificar listado admin con paths
#   - Navegar a http://localhost:8000/admin/aceptaciones
#   - Verificar que aparecen nuevas columnas:
#     * Firma Path
#     * Doc Frente Path
#     * Doc Dorso Path
#     * Audio Path
#   - Verificar que los paths se muestran (truncados si son largos)
#   - Verificar que el ID es un link clickeable
#
# PASO 4: Verificar detalle de aceptación
#   - Click en el ID de la aceptación creada (o navegar a /admin/aceptaciones/1)
#   - Verificar que se muestra:
#     * Todos los datos de la aceptación
#     * Todos los paths completos
#     * "Firma Existe: Sí" (en verde)
#     * "Doc Frente Existe: Sí" (en verde)
#     * "Doc Dorso Existe: Sí" (en verde)
#     * "Audio Existe: Sí" (en verde)
#   - Verificar link "← Volver a lista" funciona
#
# PASO 5: Probar con imagen grande (compresión)
#   - Navegar a http://localhost:8000/e/1
#   - Subir imagen de documento > 2MB pero < 4MB
#   - Verificar en logs:
#     * [request_id] Comprimiendo doc frente: ... bytes
#     * [request_id] Doc frente comprimido: ... -> ... bytes
#   - Completar y enviar formulario
#   - Verificar que la aceptación se guarda correctamente
#
# PASO 6: Probar error 413 y logging de excepciones (Mobile/Desktop)
#   - Navegar a http://localhost:8000/e/1 desde móvil o desktop
#   - Intentar subir imagen > 4MB
#   - Verificar que se rechaza con mensaje claro
#   - Verificar en logs:
#     * [request_id] Doc frente demasiado grande: ... bytes
#   - Intentar enviar firma muy grande (dibujar mucho en canvas)
#   - Verificar que se rechaza antes de enviar (validación frontend)
#   - Si se envía de alguna forma, verificar en logs:
#     * [request_id] Firma demasiado grande: ... bytes
#   - Probar con audio > 5MB
#   - Verificar en logs:
#     * [request_id] Audio demasiado grande: ... bytes
#
# VERIFICACIÓN FINAL:
#   - Revisar /var/log/encarreraok/app.log completo
#   - Verificar que todos los request_id son únicos
#   - Verificar que todos los tamaños están en bytes
#   - Verificar que todos los paths son absolutos
#   - Verificar que no hay excepciones sin loggear
#   - Verificar rotación: si el log supera 10MB, debería rotar
#
# NOTAS:
#   - Si /var/log/encarreraok no tiene permisos, el log se crea en el directorio actual
#   - Los logs incluyen timestamp, nivel, y mensaje estructurado
#   - Las excepciones incluyen stacktrace completo
#   - Los paths verificados en detalle usan os.path.exists() en tiempo real
#

