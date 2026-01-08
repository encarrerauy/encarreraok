# 📋 Guía para Actualizar main.py en Digital Ocean

**Versión:** 1.0  
**Fecha:** Diciembre 2025  
**Servidor:** Digital Ocean - Ubuntu 24.04

---

## 🎯 ¿Qué vamos a hacer?

Actualizar el archivo `main.py` en tu servidor de Digital Ocean con la última versión que está en GitHub.

---

## ⚠️ IMPORTANTE - Antes de empezar

1. **Haz un backup** del archivo actual (por si algo sale mal)
2. **Verifica** que tienes acceso al servidor
3. **Ten a mano** la contraseña o clave SSH

---

## 📍 Paso 1: Conectarse al Servidor

### Opción A: Desde la consola web de Digital Ocean
1. Ve a: https://cloud.digitalocean.com/droplets
2. Haz clic en tu droplet (encarreraok)
3. Haz clic en "Access" → "Launch Droplet Console"
4. Ya estás conectado ✅

### Opción B: Desde tu computadora (si tienes SSH configurado)
```bash
ssh root@165.22.45.221
```

---

## 🔧 Paso 1.5: Instalar Git (Si no está instalado)

### Verificar si Git ya está instalado
```bash
git --version
```

**Si ves un número de versión** (ej: `git version 2.39.2`): ✅ Git ya está instalado, puedes saltar este paso.

**Si ves "command not found"**: Necesitas instalar Git.

### Instalar Git en Ubuntu
```bash
# Actualizar lista de paquetes
apt update

# Instalar Git
apt install -y git

# Verificar instalación
git --version
```

**Deberías ver:** `git version 2.xx.x` ✅

---

## 🔍 Paso 2: Ubicación de main.py

**✅ Ya encontramos tu archivo. Está en:**

```
/var/www/encarreraok/main.py
```

**Esta es la ruta que usaremos en todos los pasos siguientes.**

---

## 💾 Paso 3: Hacer Backup (MUY IMPORTANTE)

Antes de cambiar nada, haz una copia de seguridad:

```bash
cp /var/www/encarreraok/main.py /var/www/encarreraok/main.py.backup
```

✅ **Listo, ahora tienes un backup por si algo sale mal.**

---

## 🔄 Paso 4: Actualizar el Archivo

Tienes 3 opciones. Elige la que te resulte más fácil:

---

### 🌟 OPCIÓN 1: Usando Git (Recomendada)

Esta es la forma más profesional y rápida. Sincroniza tu servidor con GitHub.

#### 4.1. Ir al directorio de la aplicación
```bash
cd /var/www/encarreraok
```

#### 4.2. Verificar si es un repositorio Git
```bash
git status
```

**CASO A: Si dice "On branch main" (o master):**
¡Genial! Solo ejecuta:
```bash
# Descargar y aplicar cambios
git pull origin main
```

**CASO B: Si dice "fatal: not a git repository":**
Significa que subiste los archivos manualmente antes. Convirtámoslo en repositorio (solo se hace una vez):
```bash
# 1. Inicializar git
git init

# 2. Configurar el origen (GitHub)
git remote add origin https://github.com/encarrerauy/encarreraok.git

# 3. Descargar la historia
git fetch origin

# 4. Forzar que tu carpeta sea idéntica a GitHub (CUIDADO: Borra cambios locales no guardados)
git reset --hard origin/main
```

**CASO C: Si hay conflictos (error al hacer pull):**
Si editaste cosas en el servidor y GitHub no te deja actualizar:
```bash
# Opción segura: Guardar tus cambios locales temporalmente
git stash
git pull origin main

# Opcion destructiva: Sobrescribir todo con lo de GitHub (recomendado si no te importan los cambios locales)
git fetch origin
git reset --hard origin/main
```

✅ **¡Listo! El archivo se actualizó.**

---

### 📤 OPCIÓN 2: Subir el archivo desde tu computadora

#### 4.1. En tu computadora (Windows)

Abre PowerShell o CMD y escribe:

```powershell
# Ir al directorio donde está main.py
cd c:\xampp\htdocs\encarreraok-v2

# Subir el archivo al servidor (reemplaza la ruta del servidor)
scp main.py root@165.22.45.221:/var/www/encarreraok/main.py
```

**Nota:** Te pedirá la contraseña del servidor.

#### 4.2. Verificar en el servidor
```bash
ls -lh /var/www/encarreraok/main.py
```

✅ **¡Listo! El archivo se actualizó.**

---

### ✏️ OPCIÓN 3: Editar directamente en el servidor

#### 4.1. Abrir el archivo con editor
```bash
nano /var/www/encarreraok/main.py
```

#### 4.2. Copiar y pegar el contenido nuevo
1. Abre `main.py` en tu computadora
2. Copia TODO el contenido (Ctrl+A, Ctrl+C)
3. En la terminal del servidor, pega el contenido (Click derecho → Paste)
4. Guardar: Presiona `Ctrl + X`, luego `Y`, luego `Enter`

✅ **¡Listo! El archivo se actualizó.**

---

## 🔄 Paso 5: Reiniciar el Servicio

Para que los cambios surtan efecto, necesitas reiniciar la aplicación:

```bash
# Reiniciar el servicio (prueba estos comandos uno por uno)
sudo systemctl restart encarreraok
# O
sudo systemctl restart uvicorn
# O
sudo systemctl restart gunicorn
```

**Si ninguno funciona, busca el nombre de tu servicio:**
```bash
sudo systemctl list-units --type=service | grep -i encarrera
```

---

## ✅ Paso 6: Verificar que Todo Funciona

### 6.1. Verificar que el servicio está corriendo
```bash
sudo systemctl status encarreraok
```

**Deberías ver:** `active (running)` en verde ✅

### 6.2. Ver los últimos logs (por si hay errores)
```bash
sudo journalctl -u encarreraok -n 50
```

### 6.3. Probar que la aplicación responde
```bash
# Probar localmente
curl http://localhost:8000/docs

# O si tienes dominio configurado
curl http://tu-dominio.com/docs
```

---

## 🆘 Solución de Problemas

### ❌ Problema: El servicio no inicia

**Solución:**
```bash
# Ver errores detallados
sudo journalctl -u encarreraok -n 100 --no-pager

# Verificar que el archivo Python no tiene errores de sintaxis
python3 -m py_compile /var/www/encarreraok/main.py
```

### ❌ Problema: No encuentro main.py

**Solución:**
```bash
# Tu archivo está en:
ls -la /var/www/encarreraok/main.py

# Si no aparece, verificar el directorio:
ls -la /var/www/encarreraok/
```

### ❌ Problema: Git no está configurado

**Solución:** Usa la OPCIÓN 2 o 3 de actualización.

### ❌ Problema: Necesito volver al archivo anterior

**Solución:**
```bash
# Restaurar desde el backup
cp /var/www/encarreraok/main.py.backup /var/www/encarreraok/main.py

# Reiniciar servicio
sudo systemctl restart encarreraok
```

---

## 📝 Resumen Rápido (Comandos para Copiar/Pegar)

**Ruta confirmada:** `/var/www/encarreraok/main.py`

```bash
# 1. Ir al directorio
cd /var/www/encarreraok

# 2. Hacer backup
cp main.py main.py.backup

# 3. Actualizar desde GitHub
git pull origin main

# 4. Reiniciar servicio
sudo systemctl restart encarreraok

# 5. Verificar
sudo systemctl status encarreraok
```

---

## 📞 Información del Servidor

- **IP Pública:** 165.22.45.221
- **IP Privada:** 10.17.0.5
- **Sistema:** Ubuntu 24.04.3 LTS
- **Usuario:** root
- **Hostname:** encarreraok

---

## 🔗 Enlaces Útiles

- **Repositorio GitHub:** https://github.com/encarrerauy/encarreraok.git
- **Panel Digital Ocean:** https://cloud.digitalocean.com/droplets

---

## 📌 Notas Finales

- ✅ **Siempre haz backup** antes de cambiar algo
- ✅ **Verifica** que el servicio está corriendo después de actualizar
- ✅ **Revisa los logs** si algo no funciona
- ✅ **Guarda esta guía** para futuras actualizaciones

---

**¿Tienes dudas?** Revisa la sección "Solución de Problemas" o consulta los logs del sistema.

**Última actualización:** Enero 2026

