import os
import io
import json
import struct
import hashlib
import logging
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

# from app.db.database import get_connection  <-- Removed, used in repo now
from app.repositories import aceptaciones_repository as repo

# Configuración de logger para este servicio
logger = logging.getLogger(__name__)

# Configuración de versiones de deslinde
LEGAL_DIR = os.environ.get("ENCARRERAOK_LEGAL_DIR", "legal")
DESLINDES_CONFIG = {
    "v1_1": "deslinde_v1_1_ligero.txt",
    "v2_0": "deslinde_v2_0_legal_fuerte.txt",
    "v3_0": "deslinde_v3_0_legal_full.txt",
}
DEFAULT_DESLINDE_VERSION = "v1_1"


def cargar_deslinde(version: str = DEFAULT_DESLINDE_VERSION) -> str:
    """
    Carga el texto del deslinde desde archivo según la versión.
    Retorna el texto base con placeholders.
    """
    filename = DESLINDES_CONFIG.get(version)
    if not filename:
        logger.error(f"Versión de deslinde desconocida: {version}, usando default")
        filename = DESLINDES_CONFIG[DEFAULT_DESLINDE_VERSION]
    
    path = os.path.join(LEGAL_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error leyendo archivo de deslinde {path}: {e}")
        # Fallback de emergencia si no se puede leer el archivo
        return """DESLINDE DE RESPONSABILIDAD Y ACEPTACIÓN DE RIESGOS

Declaro que participo en el evento deportivo {{NOMBRE_EVENTO}}, organizado por {{ORGANIZADOR}}, de manera voluntaria y bajo mi exclusiva responsabilidad.

Reconozco que la participación en actividades deportivas implica riesgos inherentes, incluyendo, pero no limitándose a, caídas, lesiones físicas, traumatismos, accidentes cardiovasculares, condiciones climáticas adversas y otros riesgos propios de la actividad.

Declaro encontrarme en condiciones físicas y de salud adecuadas para participar, y que he sido debidamente informado/a sobre las características del evento.

Eximo de toda responsabilidad civil, penal y administrativa al organizador, auspiciantes, colaboradores, personal médico, autoridades y cualquier otra persona vinculada a la organización del evento, por cualquier daño, lesión o perjuicio que pudiera sufrir antes, durante o después de mi participación.

Autorizo la utilización de mi imagen, voz y datos personales con fines de difusión, promoción y registro del evento, sin derecho a compensación económica.

Declaro haber leído, comprendido y aceptado íntegramente el presente deslinde de responsabilidad."""


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
    salud_doc_path: Optional[str] = None,
    salud_doc_tipo: Optional[str] = None,
    audio_exento: int = 0,
    firma_asistida: int = 0,
    pdf_token: Optional[str] = None,
    documento_norm: Optional[str] = None,
    deslinde_version: str = DEFAULT_DESLINDE_VERSION,
) -> int:
    """Inserta una aceptación y devuelve el ID creado."""
    return repo.crear_aceptacion(
        evento_id=evento_id,
        nombre_participante=nombre_participante,
        documento=documento,
        fecha_hora=fecha_hora,
        ip=ip,
        user_agent=user_agent,
        deslinde_hash_sha256=deslinde_hash_sha256,
        firma_path=firma_path,
        doc_frente_path=doc_frente_path,
        doc_dorso_path=doc_dorso_path,
        audio_path=audio_path,
        salud_doc_path=salud_doc_path,
        salud_doc_tipo=salud_doc_tipo,
        audio_exento=audio_exento,
        firma_asistida=firma_asistida,
        pdf_token=pdf_token,
        documento_norm=documento_norm,
        deslinde_version=deslinde_version,
    )


def aceptacion_existente(evento_id: int, documento_norm: str) -> bool:
    """
    Código defensivo para verificar duplicados.
    Delegado al repositorio.
    """
    return repo.existe_aceptacion(evento_id, documento_norm)


def listar_aceptaciones(evento_id: Optional[int] = None, query: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lista aceptaciones con datos del evento (join simple). 
    Filtra por evento si se especifica.
    Filtra por nombre o documento si query se especifica.
    """
    return repo.listar_aceptaciones(evento_id, query)


def borrar_evidencias_fisicas(aceptaciones: List[Dict[str, Any]]):
    """Borra archivos físicos de una lista de aceptaciones."""
    count = 0
    for a in aceptaciones:
        paths = [
            a.get('firma_path'),
            a.get('doc_frente_path'),
            a.get('doc_dorso_path'),
            a.get('audio_path'),
            a.get('salud_doc_path')
        ]
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    count += 1
                except OSError as e:
                    logger.error(f"Error borrando archivo {p}: {e}")
    return count


def eliminar_aceptaciones_por_ids(ids: List[int]) -> int:
    """Elimina registros de aceptaciones por lista de IDs."""
    return repo.eliminar_aceptaciones_por_ids(ids)


def get_aceptacion_detalle(aceptacion_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene detalle completo de una aceptación con verificación de existencia de archivos."""
    data = repo.buscar_aceptacion_por_id(aceptacion_id)
    if not data:
        return None
    
    # Verificar existencia de archivos
    data['firma_exists'] = os.path.exists(data['firma_path']) if data.get('firma_path') else False
    data['doc_frente_exists'] = os.path.exists(data['doc_frente_path']) if data.get('doc_frente_path') else False
    data['doc_dorso_exists'] = os.path.exists(data['doc_dorso_path']) if data.get('doc_dorso_path') else False
    data['audio_exists'] = os.path.exists(data['audio_path']) if data.get('audio_path') else False
    data['salud_doc_exists'] = os.path.exists(data['salud_doc_path']) if data.get('salud_doc_path') else False
    
    return data


def get_aceptacion_por_token(pdf_token: str) -> Optional[Dict[str, Any]]:
    """Obtiene aceptación por token público."""
    return repo.buscar_aceptacion_por_token(pdf_token)


def revocar_pdf_token(aceptacion_id: int) -> bool:
    """Revoca el token PDF de una aceptación (soft revoke)."""
    return repo.revocar_pdf_token(aceptacion_id)


def registrar_acceso_pdf(aceptacion_id: int):
    """Registra un acceso exitoso al PDF."""
    try:
        now_utc = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        repo.registrar_acceso_pdf(aceptacion_id, now_utc)
    except Exception as e:
        logger.error(f"Error registrando acceso PDF id={aceptacion_id}: {e}")



def calcular_hash_sha256(texto: str) -> str:
    """Calcula SHA256 en hex del texto provisto."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def calcular_hash_archivo(filepath: str) -> str:
    """Calcula SHA256 de un archivo en disco."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Leer en chunks para eficiencia
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_deslinde_activo(evento_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene el deslinde activo para un evento."""
    return repo.get_deslinde_activo(evento_id)


def insertar_deslinde(
    evento_id: int,
    texto: str,
    activo: int = 1,
    creado_por: str = "sistema",
) -> int:
    """Inserta un deslinde para un evento (por defecto activo) y devuelve su ID."""
    hashv = calcular_hash_sha256(texto)
    fecha_creacion = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return repo.insertar_deslinde(
        evento_id=evento_id,
        texto=texto,
        hash_sha256=hashv,
        fecha_creacion=fecha_creacion,
        activo=activo,
        creado_por=creado_por
    )


# ------------------------------------------------------------------------------
# Generador PDF con soporte Unicode (TTF Embed + Identity-H)
# ------------------------------------------------------------------------------
class TTFFont:
    """
    Parser minimalista de archivos TTF para extracción de métricas y mapeo Unicode.
    Soporta tablas: head, hhea, hmtx, cmap (format 4).
    """
    def __init__(self, font_path: str):
        with open(font_path, 'rb') as f:
            self.data = f.read()
        
        self.tables = {}
        self.units_per_em = 1000
        self.ascent = 0
        self.descent = 0
        self.cap_height = 0
        self.bbox = [0, 0, 0, 0]
        self.advance_widths = []
        self.cmap = {}  # unicode -> gid
        self.gid_to_unicode = {} # gid -> unicode
        self.num_metrics = 0
        
        self._parse()

    def _parse(self):
        # Offset Table
        num_tables = struct.unpack('>H', self.data[4:6])[0]
        offset = 12
        for _ in range(num_tables):
            tag = self.data[offset:offset+4].decode('latin1')
            checksum, t_offset, t_length = struct.unpack('>III', self.data[offset+4:offset+16])
            self.tables[tag] = (t_offset, t_length)
            offset += 16
            
        self._parse_head()
        self._parse_hhea()
        self._parse_hmtx()
        self._parse_cmap()

    def _parse_head(self):
        if 'head' not in self.tables: return
        off, _ = self.tables['head']
        self.units_per_em = struct.unpack('>H', self.data[off+18:off+20])[0]
        x_min, y_min, x_max, y_max = struct.unpack('>hhhh', self.data[off+36:off+44])
        self.bbox = [x_min, y_min, x_max, y_max]

    def _parse_hhea(self):
        if 'hhea' not in self.tables: return
        off, _ = self.tables['hhea']
        self.ascent, self.descent = struct.unpack('>hh', self.data[off+4:off+8])
        self.num_metrics = struct.unpack('>H', self.data[off+34:off+36])[0]

    def _parse_hmtx(self):
        if 'hmtx' not in self.tables: return
        off, _ = self.tables['hmtx']
        # Read advance widths
        self.advance_widths = []
        for i in range(self.num_metrics):
            aw, lsb = struct.unpack('>Hh', self.data[off + i*4 : off + i*4 + 4])
            self.advance_widths.append(aw)
        
        # We don't read LSBs for trailing glyphs to save memory/time, 
        # usually assume last width for remaining glyphs (monospaced logic) or 0
        
    def _parse_cmap(self):
        if 'cmap' not in self.tables: return
        off, _ = self.tables['cmap']
        num_subtables = struct.unpack('>H', self.data[off+2:off+4])[0]
        
        subtable_offset = 0
        for i in range(num_subtables):
            platform_id, encoding_id, s_off = struct.unpack('>HHI', self.data[off+4 + i*8 : off+4 + i*8 + 8])
            # Prefer Windows Unicode (3, 1) or (3, 10)
            if platform_id == 3 and encoding_id in (1, 10):
                subtable_offset = off + s_off
                break
            # Fallback to Unicode Platform (0, *)
            if platform_id == 0:
                subtable_offset = off + s_off
        
        if subtable_offset == 0: return

        format = struct.unpack('>H', self.data[subtable_offset:subtable_offset+2])[0]
        if format == 4:
            self._parse_cmap_format_4(subtable_offset)

    def _parse_cmap_format_4(self, offset):
        length = struct.unpack('>H', self.data[offset+2:offset+4])[0]
        seg_count_x2 = struct.unpack('>H', self.data[offset+6:offset+8])[0]
        seg_count = seg_count_x2 // 2
        
        end_counts = []
        for i in range(seg_count):
            pos = offset + 14 + i*2
            end_counts.append(struct.unpack('>H', self.data[pos:pos+2])[0])
            
        start_counts = []
        for i in range(seg_count):
            pos = offset + 14 + seg_count*2 + 2 + i*2
            start_counts.append(struct.unpack('>H', self.data[pos:pos+2])[0])

        id_deltas = []
        for i in range(seg_count):
            pos = offset + 14 + seg_count*4 + 2 + i*2
            # Signed short
            delta = struct.unpack('>h', self.data[pos:pos+2])[0]
            id_deltas.append(delta)
            
        id_range_offsets = []
        for i in range(seg_count):
            pos = offset + 14 + seg_count*6 + 2 + i*2
            id_range_offsets.append(struct.unpack('>H', self.data[pos:pos+2])[0])
            
        # Build map
        for i in range(seg_count):
            start = start_counts[i]
            end = end_counts[i]
            delta = id_deltas[i]
            range_offset = id_range_offsets[i]
            
            for c in range(start, end + 1):
                gid = 0
                if range_offset == 0:
                    gid = (c + delta) & 0xFFFF
                else:
                    # Address arithmetic
                    # offset of range_offset array element
                    ro_pos = offset + 14 + seg_count*6 + 2 + i*2
                    # glyph_index_address = ro_pos + range_offset + (c - start) * 2
                    glyph_index_addr = ro_pos + range_offset + (c - start) * 2
                    if glyph_index_addr + 2 <= offset + length:
                        gid = struct.unpack('>H', self.data[glyph_index_addr:glyph_index_addr+2])[0]
                        if gid != 0:
                            gid = (gid + delta) & 0xFFFF
                
                if gid != 0:
                    self.cmap[c] = gid
                    self.gid_to_unicode[gid] = c

    def get_gid(self, unicode_code):
        return self.cmap.get(unicode_code, 0) # 0 = .notdef

    def get_width(self, gid):
        if gid < len(self.advance_widths):
            return self.advance_widths[gid]
        # Fallback to last known width
        if self.advance_widths:
            return self.advance_widths[-1]
        return 1000 # Fallback default


class SimplePDFGenerator:
    """
    Generador de PDF 1.4 con soporte Unicode real (TTF Embed + Identity-H).
    Reemplaza la implementación anterior WinAnsi para cumplir P0.6.
    """
    def __init__(self):
        self.buffer = io.BytesIO()
        self.pages_content = []
        self.current_content = []
        self.obj_offsets = []
        self.obj_count = 0
        
        # Configuración página Letter (612x792 pt)
        self.page_width = 612
        self.page_height = 792
        self.margin_left = 50
        self.margin_top = 50
        self.y = self.page_height - self.margin_top
        
        # Fuente TTF
        self.font_path = "assets/fonts/DejaVuSans.ttf"
        try:
            self.font = TTFFont(self.font_path)
            self.font_loaded = True
        except Exception as e:
            # Fallback a modo seguro si falla carga (aunque no debería)
            print(f"Error cargando fuente: {e}")
            self.font_loaded = False
            
        self.font_size = 10
        self.line_height = 12
        
        # Tracking de GIDs usados para optimizar PDF (ToUnicode/Widths)
        self.used_gids = set()
        self.used_gids.add(0) # .notdef
        
        # Inicializar primera página
        self._init_page_state()
    
    def _init_page_state(self):
        self.current_content.append(f"BT /F1 {self.font_size} Tf\n".encode('ascii'))

    def _add_page(self):
        if self.current_content:
            self.current_content.append(b"ET\n")
            self.pages_content.append(b"".join(self.current_content))
        self.current_content = []
        self.y = self.page_height - self.margin_top
        self._init_page_state()

    def set_font_size(self, size: int):
        self.font_size = size
        self.line_height = int(size * 1.2)
        if self.current_content:
             self.current_content.append(f"/F1 {self.font_size} Tf\n".encode('ascii'))

    def add_text(self, text: str):
        """Agrega texto manejando saltos de línea y paginación con métricas reales."""
        # 1. Convertir texto a GIDs y calcular anchos
        gids = []
        words = [] # Lista de (palabra_gids, ancho)
        
        # Normalización básica: reemplazar newlines y tabs
        lines = text.split('\n')
        
        scale = self.font_size / self.font.units_per_em if self.font_loaded else 0.001
        max_width = self.page_width - 2 * self.margin_left
        
        for line_text in lines:
            current_line_gids = []
            current_line_width = 0
            
            # Procesar por palabras para wrapping
            # Split manual preservando espacios no es trivial, simplificamos:
            # Vamos caracter a caracter acumulando en linea
            
            # Mejor aproximación: split por espacio
            words_in_line = line_text.split(' ')
            
            for i, word in enumerate(words_in_line):
                word_gids = []
                word_width = 0
                
                # Agregar espacio previo si no es la primera palabra
                if i > 0:
                    space_gid = self.font.get_gid(32)
                    self.used_gids.add(space_gid)
                    w = self.font.get_width(space_gid) * scale
                    word_gids.append(space_gid)
                    word_width += w
                
                # Caracteres de la palabra
                for char in word:
                    gid = self.font.get_gid(ord(char))
                    self.used_gids.add(gid)
                    w = self.font.get_width(gid) * scale
                    word_gids.append(gid)
                    word_width += w
                
                # Check wrap
                if current_line_width + word_width > max_width and current_line_gids:
                    # Flush current line
                    self._write_line_gids(current_line_gids)
                    current_line_gids = []
                    current_line_width = 0
                    # Si era espacio inicial, quitarlo para nueva linea
                    if word_gids and word_gids[0] == self.font.get_gid(32):
                        w_space = self.font.get_width(self.font.get_gid(32)) * scale
                        word_gids.pop(0)
                        word_width -= w_space
                
                current_line_gids.extend(word_gids)
                current_line_width += word_width
                
            self._write_line_gids(current_line_gids)

    def _write_line_gids(self, gids: List[int]):
        if not gids: return
        
        if self.y < self.margin_top:
            self._add_page()
            
        # Convert gids to big-endian hex string
        hex_str = "".join([f"{gid:04X}" for gid in gids])
        
        # Posicionar texto: 1 0 0 1 x y Tm
        cmd = f"1 0 0 1 {self.margin_left} {self.y} Tm <{hex_str}> Tj\n"
        self.current_content.append(cmd.encode('ascii'))
        self.y -= self.line_height

    def get_pdf_bytes(self) -> bytes:
        # Cerrar última página
        if self.current_content:
            self.current_content.append(b"ET\n")
            self.pages_content.append(b"".join(self.current_content))
        
        if not self.pages_content:
            # Pagina vacia dummy
            self.pages_content.append(b"BT /F1 12 Tf ET\n")

        self.buffer = io.BytesIO()
        self.obj_offsets = []
        self.obj_count = 0
        
        def write(data: bytes):
            self.buffer.write(data)

        def start_obj():
            self.obj_count += 1
            self.obj_offsets.append(self.buffer.tell())
            write(f"{self.obj_count} 0 obj\n".encode('ascii'))
            return self.obj_count

        def end_obj():
            write(b"\nendobj\n")

        # Header
        write(b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n")
        
        # IDs
        catalog_id = 1
        pages_root_id = 2
        font_id = 3
        
        # 1. Catalog
        start_obj() # ID 1
        write(f"<< /Type /Catalog /Pages {pages_root_id} 0 R >>".encode('ascii'))
        end_obj()
        
        num_pages = len(self.pages_content)
        # IDs dinámicos
        # 1: Catalog, 2: Pages, 3: Type0 Font, 4: CIDFont, 5: FontDesc, 6: ToUnicode, 7: FontFile
        cid_font_id = 4
        font_desc_id = 5
        to_unicode_id = 6
        font_file_id = 7
        
        first_page_id = 8
        first_content_id = first_page_id + num_pages
        
        # 2. Pages Root
        start_obj() # ID 2
        kids_refs = [f"{first_page_id + i} 0 R" for i in range(num_pages)]
        write(f"<< /Type /Pages /Kids [{' '.join(kids_refs)}] /Count {num_pages} >>".encode('ascii'))
        end_obj()
        
        # 3. Type0 Font (Composite)
        start_obj() # ID 3
        write(f"""<< 
/Type /Font 
/Subtype /Type0 
/BaseFont /DejaVuSans 
/Encoding /Identity-H 
/DescendantFonts [{cid_font_id} 0 R] 
/ToUnicode {to_unicode_id} 0 R 
>>""".encode('ascii'))
        end_obj()
        
        # 4. CIDFontType2
        start_obj() # ID 4
        # Construir array de anchos (W)
        # Formato: [ first_gid [ w1 w2 ... ] ... ]
        # Para simplificar, agrupamos por rangos consecutivos o dumpamos todo si no es muy grande.
        # Solo necesitamos anchos de los usados.
        sorted_gids = sorted(list(self.used_gids))
        w_array = []
        if sorted_gids:
            # Algoritmo simple: bloques consecutivos
            current_block = []
            block_start = sorted_gids[0]
            prev_gid = block_start - 1
            
            for gid in sorted_gids:
                if gid != prev_gid + 1:
                    # Cerrar bloque anterior
                    w_array.append(f"{block_start} [{' '.join(map(str, current_block))}]")
                    current_block = []
                    block_start = gid
                
                current_block.append(self.font.get_width(gid))
                prev_gid = gid
            
            if current_block:
                w_array.append(f"{block_start} [{' '.join(map(str, current_block))}]")
        
        w_str = " ".join(w_array)
        
        write(f"""<< 
/Type /Font 
/Subtype /CIDFontType2 
/BaseFont /DejaVuSans 
/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> 
/FontDescriptor {font_desc_id} 0 R 
/DW 1000 
/W [{w_str}] 
>>""".encode('ascii'))
        end_obj()
        
        # 5. FontDescriptor
        start_obj() # ID 5
        # Flags 4 = Symbolic
        write(f"""<< 
/Type /FontDescriptor 
/FontName /DejaVuSans 
/Flags 4 
/FontBBox [{self.font.bbox[0]} {self.font.bbox[1]} {self.font.bbox[2]} {self.font.bbox[3]}] 
/ItalicAngle 0 
/Ascent {self.font.ascent} 
/Descent {self.font.descent} 
/CapHeight {self.font.cap_height} 
/StemV 80 
/FontFile2 {font_file_id} 0 R 
>>""".encode('ascii'))
        end_obj()
        
        # 6. ToUnicode CMap
        start_obj() # ID 6
        # Generar CMap para copiar texto
        cmap_lines = []
        cmap_lines.append("/CIDInit /ProcSet findresource begin")
        cmap_lines.append("12 dict begin")
        cmap_lines.append("begincmap")
        cmap_lines.append("/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def")
        cmap_lines.append("/CMapName /Adobe-Identity-UCS def")
        cmap_lines.append("/CMapType 2 def")
        cmap_lines.append("1 begincodespacerange")
        cmap_lines.append("<0000> <FFFF>")
        cmap_lines.append("endcodespacerange")
        
        # bfchar lines
        # Group in chunks of 100
        chunk_size = 100
        gids_list = list(self.used_gids)
        for i in range(0, len(gids_list), chunk_size):
            chunk = gids_list[i:i+chunk_size]
            cmap_lines.append(f"{len(chunk)} beginbfchar")
            for gid in chunk:
                uni = self.font.gid_to_unicode.get(gid, 0)
                # UTF-16BE hex
                uni_hex = f"{uni:04X}"
                cmap_lines.append(f"<{gid:04X}> <{uni_hex}>")
            cmap_lines.append("endbfchar")
            
        cmap_lines.append("endcmap")
        cmap_lines.append("CMapName currentdict /CMap defineresource pop")
        cmap_lines.append("end")
        cmap_lines.append("end")
        
        cmap_data = "\n".join(cmap_lines).encode('ascii')
        write(f"<< /Length {len(cmap_data)} >>\nstream\n".encode('ascii'))
        write(cmap_data)
        write(b"\nendstream")
        end_obj()
        
        # 7. FontFile2 (Embedded TTF)
        start_obj() # ID 7
        write(f"<< /Length {len(self.font.data)} >>\nstream\n".encode('ascii'))
        write(self.font.data)
        write(b"\nendstream")
        end_obj()
        
        # Pages and Content
        for i, content in enumerate(self.pages_content):
            page_id = first_page_id + i
            content_id = first_content_id + i
            
            # Page Object
            start_obj() 
            write(f"<< /Type /Page /Parent {pages_root_id} 0 R /MediaBox [0 0 {self.page_width} {self.page_height}] /Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode('ascii'))
            end_obj()
            
        for i, content in enumerate(self.pages_content):
            # Content Stream Object
            start_obj() 
            write(f"<< /Length {len(content)} >>\nstream\n".encode('ascii'))
            write(content)
            write(b"\nendstream")
            end_obj()
            
        # Xref
        xref_offset = self.buffer.tell()
        write(b"xref\n")
        write(f"0 {self.obj_count + 1}\n".encode('ascii'))
        write(b"0000000000 65535 f \n")
        for offset in self.obj_offsets:
            write(f"{offset:010d} 00000 n \n".encode('ascii'))
            
        # Trailer
        write(b"trailer\n")
        write(f"<< /Size {self.obj_count + 1} /Root {catalog_id} 0 R >>\n".encode('ascii'))
        write(b"startxref\n")
        write(f"{xref_offset}\n".encode('ascii'))
        write(b"%%EOF\n")
        
        return self.buffer.getvalue()


def generar_pdf_aceptacion(aceptacion: Dict[str, Any], evento: Dict[str, Any]) -> bytes:
    """Helper para generar el PDF legal de una aceptación."""
    # Reconstruir texto deslinde
    version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
    texto_base = cargar_deslinde(version)
    texto_final = texto_base.replace("{{NOMBRE_EVENTO}}", evento["nombre"])\
                            .replace("{{ORGANIZADOR}}", evento["organizador"])

    # Verificación de consistencia de hash
    hash_calculado = calcular_hash_sha256(texto_final)
    hash_bd = aceptacion['deslinde_hash_sha256']
    consistencia = "OK" if hash_calculado == hash_bd else "NO COINCIDE"

    if consistencia != "OK":
        logger.warning(f"Inconsistencia de hash detectada - AceptacionID: {aceptacion['id']}, EventoID: {evento['id']}. BD: {hash_bd}, Calc: {hash_calculado}")

    # Generar PDF
    pdf = SimplePDFGenerator()
    
    # Encabezado
    pdf.set_font_size(14)
    pdf.add_text("ACEPTACIÓN DE DESLINDE DE RESPONSABILIDAD")
    pdf.set_font_size(10)
    pdf.add_text(f"ID Aceptación: {aceptacion['id']}")
    pdf.add_text(f"Fecha y hora de generación del documento (UTC): {datetime.utcnow().replace(microsecond=0).isoformat()}Z")
    pdf.add_text("\n")
    
    # Evento
    pdf.set_font_size(12)
    pdf.add_text("EVENTO")
    pdf.set_font_size(10)
    pdf.add_text(f"Nombre: {evento['nombre']}")
    pdf.add_text(f"Fecha: {evento['fecha']}")
    pdf.add_text(f"Organizador: {evento['organizador']}")
    pdf.add_text("\n")
    
    # Participante
    pdf.set_font_size(12)
    pdf.add_text("PARTICIPANTE")
    pdf.set_font_size(10)
    pdf.add_text(f"Nombre: {aceptacion['nombre_participante']}")
    pdf.add_text(f"Documento: {aceptacion['documento']}")
    pdf.add_text("\n")
    
    # Texto Legal
    pdf.set_font_size(12)
    pdf.add_text("TEXTO DEL DESLINDE ACEPTADO")
    pdf.add_text("-" * 60) # Separador visual
    pdf.set_font_size(9)
    pdf.add_text(texto_final)
    pdf.add_text("-" * 60)
    pdf.add_text("\n")
    
    # Auditoría
    pdf.set_font_size(12)
    pdf.add_text("AUDITORÍA TÉCNICA")
    pdf.set_font_size(10)
    pdf.add_text(f"Hash SHA256 Deslinde (BD): {hash_bd}")
    pdf.add_text(f"Hash SHA256 Calculado: {hash_calculado}")
    pdf.add_text(f"Consistencia del hash: {consistencia}")
    pdf.add_text(f"Fecha Aceptación (UTC): {aceptacion['fecha_hora']}")
    pdf.add_text(f"Dirección IP: {aceptacion['ip']}")
    pdf.add_text(f"User-Agent: {aceptacion['user_agent']}")
    
    # Bloque de evidencias solicitadas (P0.5)
    firma_status = "PRESENTE" if aceptacion.get('firma_path') else "NO APLICA"
    
    doc_status = "NO APLICA"
    if aceptacion.get('doc_frente_path') and aceptacion.get('doc_dorso_path'):
        doc_status = "PRESENTE"
    elif aceptacion.get('doc_frente_path') or aceptacion.get('doc_dorso_path'):
        doc_status = "PARCIAL"
        
    audio_status = "NO APLICA"
    if aceptacion.get('audio_path'):
        audio_status = "PRESENTE"
    elif aceptacion.get('audio_exento'):
        audio_status = "EXENTO"
        
    salud_status = "PRESENTE" if aceptacion.get('salud_doc_path') else "NO APLICA"
    
    pdf.add_text("\n")
    pdf.add_text("EVIDENCIAS ADJUNTAS (VERIFICAR EN SISTEMA):")
    pdf.add_text(f"- Firma Manuscrita (Fichero): {firma_status}")
    pdf.add_text(f"- Documento Identidad (Frente/Dorso): {doc_status}")
    pdf.add_text(f"- Audio Aceptación: {audio_status}")
    pdf.add_text(f"- Documento Salud: {salud_status}")
    pdf.add_text("\n")
    
    # Documento de salud (detalle extra)
    tiene_salud = "Sí" if aceptacion.get('salud_doc_path') else "No"
    pdf.add_text(f"Documento de salud aportado: {tiene_salud}")
    if aceptacion.get('salud_doc_path'):
        pdf.add_text(f"Tipo de documento: {aceptacion.get('salud_doc_tipo', 'No especificado')}")
    
    flags = []
    if aceptacion.get('audio_exento'): flags.append("AUDIO_EXENTO")
    if aceptacion.get('firma_asistida'): flags.append("FIRMA_ASISTIDA")
    if flags:
        pdf.add_text(f"Flags: {', '.join(flags)}")
    
    pdf.add_text("\n")
    pdf.set_font_size(8)
    pdf.add_text("Documento generado automáticamente por el sistema EncarreraOK.")
    
    return pdf.get_pdf_bytes()
