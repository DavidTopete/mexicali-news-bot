import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
from io import BytesIO
from PIL import Image, ImageOps
import time
import json
import os
import re
import logging
from difflib import SequenceMatcher


# ============================================================
# CONFIGURACIÓN
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

log = logging.getLogger("mexicali_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas_mexicali.json"

# Mexicali usa America/Tijuana por cambios estacionales de horario.
TZ = ZoneInfo("America/Tijuana")

UMBRAL_SIMILITUD_TITULO = 0.80
MAX_HISTORIAL = 1000
MAX_NOTICIAS_POR_CORRIDA = 10
ACEPTAR_HOY_Y_AYER = True
MAX_IMAGE_BYTES = 18 * 1024 * 1024


# ============================================================
# FUENTES
# ============================================================
#
# Rutas actualizadas:
# - La Voz de la Frontera publica artículos bajo /lavozdelafrontera/...
# - El Imparcial mantiene la portada /mexicali/ pero los artículos actuales
#   usan /mxl/mexicali/YYYY/MM/DD/...
# - La Crónica se conserva como tercera fuente.
# ============================================================

FUENTES = [
    {
        "nombre": "La Voz de la Frontera",
        "url": "https://www.lavozdelafrontera.com.mx/local/"
    },
    {
        "nombre": "La Voz de la Frontera - Policiaca",
        "url": "https://www.lavozdelafrontera.com.mx/policiaca/"
    },
    {
        "nombre": "El Imparcial Mexicali",
        "url": "https://www.elimparcial.com/mexicali/"
    },
    {
        "nombre": "La Crónica Mexicali",
        "url": "https://www.lacronica.com/mexicali/"
    }
]

LIMITE_POR_FUENTE = {
    "La Voz de la Frontera": 4,
    "La Voz de la Frontera - Policiaca": 2,
    "El Imparcial Mexicali": 4,
    "La Crónica Mexicali": 3
}


# ============================================================
# HEADERS / SESIÓN
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# UTILIDADES
# ============================================================

def limpiar_texto(texto):
    texto = str(texto or "").lower()

    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n"
    }

    for original, reemplazo in reemplazos.items():
        texto = texto.replace(original, reemplazo)

    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def escapar_html(texto):
    texto = str(texto or "")

    return (
        texto
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def titulo_parecido(t1, t2):
    a = limpiar_texto(t1)
    b = limpiar_texto(t2)

    if not a or not b:
        return False

    return SequenceMatcher(
        None,
        a,
        b
    ).ratio() >= UMBRAL_SIMILITUD_TITULO


# ============================================================
# HISTORIAL
# ============================================================

def historial_vacio():
    return {
        "ultima_ejecucion": None,
        "ultimo_total_encontrado": 0,
        "ultimo_total_enviado": 0,
        "links": [],
        "titulos": []
    }


def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return historial_vacio()

    try:
        with open(
            ARCHIVO_ENVIADAS,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return historial_vacio()

        base = historial_vacio()
        base.update(data)

        if not isinstance(base.get("links"), list):
            base["links"] = []

        if not isinstance(base.get("titulos"), list):
            base["titulos"] = []

        return base

    except Exception as error:
        log.error(
            f"Error leyendo historial: {error}"
        )

        try:
            respaldo = (
                f"{ARCHIVO_ENVIADAS}"
                f".bak_{int(time.time())}"
            )

            os.replace(
                ARCHIVO_ENVIADAS,
                respaldo
            )

            log.warning(
                f"Historial respaldado como {respaldo}"
            )

        except OSError:
            pass

        return historial_vacio()


def guardar_enviadas_en_disco(historial):
    historial["links"] = (
        historial.get("links", [])[-MAX_HISTORIAL:]
    )

    historial["titulos"] = (
        historial.get("titulos", [])[-MAX_HISTORIAL:]
    )

    temporal = ARCHIVO_ENVIADAS + ".tmp"

    with open(
        temporal,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            historial,
            f,
            ensure_ascii=False,
            indent=2
        )
        f.write("\n")

    os.replace(
        temporal,
        ARCHIVO_ENVIADAS
    )


class Historial:

    def __init__(self):
        data = cargar_enviadas()

        self.links = set(
            data["links"]
        )

        self.titulos = list(
            data["titulos"]
        )

        self.ultima_ejecucion = data.get(
            "ultima_ejecucion"
        )

        self.ultimo_total_encontrado = data.get(
            "ultimo_total_encontrado",
            0
        )

        self.ultimo_total_enviado = data.get(
            "ultimo_total_enviado",
            0
        )

    def ya_fue_enviada(self, noticia):
        if noticia["link"] in self.links:
            return True

        return any(
            titulo_parecido(
                noticia["titulo"],
                titulo_guardado
            )
            for titulo_guardado in self.titulos
        )

    def registrar(self, noticia):
        self.links.add(
            noticia["link"]
        )

        if noticia["titulo"] not in self.titulos:
            self.titulos.append(
                noticia["titulo"]
            )

    def guardar(
        self,
        encontrados=None,
        enviados=None
    ):
        if encontrados is not None:
            self.ultimo_total_encontrado = encontrados

        if enviados is not None:
            self.ultimo_total_enviado = enviados

        self.ultima_ejecucion = (
            datetime.now(TZ).isoformat()
        )

        guardar_enviadas_en_disco({
            "ultima_ejecucion":
                self.ultima_ejecucion,

            "ultimo_total_encontrado":
                self.ultimo_total_encontrado,

            "ultimo_total_enviado":
                self.ultimo_total_enviado,

            "links":
                list(self.links),

            "titulos":
                self.titulos
        })

        log.info(
            f"Historial guardado: "
            f"{len(self.links)} links, "
            f"{len(self.titulos)} títulos"
        )


# ============================================================
# FILTRO GEOGRÁFICO
# ============================================================

def es_noticia_mexicali(titulo, link):
    texto = limpiar_texto(
        f"{titulo} {link}"
    )

    claves_mexicali = [
        "mexicali",
        "valle de mexicali",
        "cachanilla",
        "palaco",
        "calexico",
        "nuevo mexicali",
        "pueblo nuevo",
        "zona centro",
        "garita",
        "aduana"
    ]

    excluir = [
        "tijuana",
        "ensenada",
        "rosarito",
        "tecate",
        "san felipe",
        "san quintin",
        "hermosillo",
        "slrc",
        "san luis rio colorado",
        "nogales",
        "obregon",
        "guaymas"
    ]

    for ciudad in excluir:
        if ciudad in texto and "mexicali" not in texto:
            return False

    return any(
        clave in texto
        for clave in claves_mexicali
    )


# ============================================================
# FECHAS
# ============================================================

def convertir_fecha(fecha_texto):
    if not fecha_texto:
        return None

    fecha_texto = str(
        fecha_texto
    ).strip()

    try:
        if fecha_texto.endswith("Z"):
            fecha_texto = (
                fecha_texto[:-1]
                + "+00:00"
            )

        fecha = datetime.fromisoformat(
            fecha_texto
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(
                tzinfo=TZ
            )

        return fecha.astimezone(TZ)

    except (ValueError, TypeError):
        pass

    for formato in [
        "%Y-%m-%d",
        "%d/%m/%Y"
    ]:
        try:
            fecha = datetime.strptime(
                fecha_texto,
                formato
            )

            return fecha.replace(
                tzinfo=TZ
            )

        except (ValueError, TypeError):
            continue

    return None


def extraer_fecha_desde_soup(soup):
    metas = [
        {"property": "article:published_time"},
        {"property": "article:modified_time"},
        {"name": "date"},
        {"name": "pubdate"},
        {"name": "publishdate"},
        {"name": "timestamp"},
        {"itemprop": "datePublished"},
        {"itemprop": "dateModified"}
    ]

    for meta_info in metas:
        meta = soup.find(
            "meta",
            attrs=meta_info
        )

        if (
            meta
            and meta.get("content")
        ):
            fecha = convertir_fecha(
                meta.get("content")
            )

            if fecha:
                return fecha

    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    for script in scripts:
        texto = script.get_text(
            " ",
            strip=True
        )

        coincidencias = re.findall(
            r'"datePublished"\s*:\s*"([^"]+)"',
            texto
        )

        for fecha_texto in coincidencias:
            fecha = convertir_fecha(
                fecha_texto
            )

            if fecha:
                return fecha

    return None


def fecha_aceptable(fecha):
    if not fecha:
        return True

    hoy = datetime.now(TZ).date()
    ayer = hoy - timedelta(days=1)

    if ACEPTAR_HOY_Y_AYER:
        return fecha.date() in (
            hoy,
            ayer
        )

    return fecha.date() == hoy


# ============================================================
# DETECCIÓN DE IMAGEN
# ============================================================

def obtener_meta_content(
    soup,
    property_name=None,
    name=None,
    itemprop=None
):
    attrs = {}

    if property_name:
        attrs["property"] = property_name

    if name:
        attrs["name"] = name

    if itemprop:
        attrs["itemprop"] = itemprop

    meta = soup.find(
        "meta",
        attrs=attrs
    )

    if meta and meta.get("content"):
        return meta.get("content").strip()

    return None


def extraer_imagen_json_ld(
    soup,
    article_url
):
    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    patrones = [
        r'"image"\s*:\s*"([^"]+)"',
        r'"thumbnailUrl"\s*:\s*"([^"]+)"',
        r'"image"\s*:\s*\[\s*"([^"]+)"',
        r'"image"\s*:\s*\{[^{}]*?"url"\s*:\s*"([^"]+)"'
    ]

    for script in scripts:
        texto = script.get_text(
            " ",
            strip=True
        )

        for patron in patrones:
            match = re.search(
                patron,
                texto,
                flags=re.I
            )

            if match:
                valor = (
                    match.group(1)
                    .replace("\\/", "/")
                )

                return urljoin(
                    article_url,
                    valor
                )

    return None


def escoger_de_srcset(
    srcset,
    article_url
):
    if not srcset:
        return None

    candidatos = []

    for fragmento in srcset.split(","):
        fragmento = fragmento.strip()

        if not fragmento:
            continue

        partes = fragmento.split()

        url = partes[0].strip()
        peso = 0

        if len(partes) > 1:
            numeros = re.findall(
                r"\d+",
                partes[-1]
            )

            if numeros:
                peso = int(
                    numeros[0]
                )

        candidatos.append(
            (
                peso,
                urljoin(
                    article_url,
                    url
                )
            )
        )

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return candidatos[0][1]


def parece_imagen_util(url):
    if not url:
        return False

    texto = limpiar_texto(url)

    excluir = [
        "logo",
        "icon",
        "avatar",
        "sprite",
        "favicon",
        "placeholder",
        "blank",
        "pixel",
        "tracking",
        "banner",
        "ads"
    ]

    return not any(
        palabra in texto
        for palabra in excluir
    )


def extraer_imagen_desde_soup(
    soup,
    article_url
):
    """
    Busca imagen en:
      1. og:image
      2. og:image:secure_url
      3. twitter:image
      4. twitter:image:src
      5. itemprop=image
      6. JSON-LD
      7. article img / srcset / data-src
      8. main img
      9. cualquier img útil
    """

    candidatos_meta = [
        obtener_meta_content(
            soup,
            property_name="og:image"
        ),
        obtener_meta_content(
            soup,
            property_name="og:image:secure_url"
        ),
        obtener_meta_content(
            soup,
            name="twitter:image"
        ),
        obtener_meta_content(
            soup,
            name="twitter:image:src"
        ),
        obtener_meta_content(
            soup,
            itemprop="image"
        )
    ]

    for candidato in candidatos_meta:
        if candidato:
            url = urljoin(
                article_url,
                candidato
            )

            if parece_imagen_util(url):
                return url

    json_ld = extraer_imagen_json_ld(
        soup,
        article_url
    )

    if (
        json_ld
        and parece_imagen_util(json_ld)
    ):
        return json_ld

    contenedores = []

    article = soup.find("article")

    if article:
        contenedores.append(article)

    main = soup.find("main")

    if main:
        contenedores.append(main)

    contenedores.append(soup)

    atributos_directos = [
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-url",
        "src"
    ]

    for contenedor in contenedores:
        for img in contenedor.find_all("img"):

            for atributo_srcset in [
                "data-srcset",
                "srcset"
            ]:
                url_srcset = escoger_de_srcset(
                    img.get(
                        atributo_srcset
                    ),
                    article_url
                )

                if (
                    url_srcset
                    and parece_imagen_util(
                        url_srcset
                    )
                ):
                    return url_srcset

            for atributo in atributos_directos:
                valor = img.get(
                    atributo
                )

                if not valor:
                    continue

                url = urljoin(
                    article_url,
                    valor.strip()
                )

                if parece_imagen_util(url):
                    return url

    return None


# ============================================================
# DESCARGA / CONVERSIÓN DE IMAGEN
# ============================================================

def descargar_imagen(
    image_url,
    article_url
):
    """
    El bot descarga la imagen con headers de navegador y el artículo
    como Referer, la convierte a JPEG y después la sube a Telegram.
    """

    if not image_url:
        return None

    headers_imagen = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": (
            "image/avif,image/webp,image/apng,"
            "image/svg+xml,image/*,*/*;q=0.8"
        ),
        "Accept-Language":
            HEADERS["Accept-Language"],
        "Referer": article_url
    }

    try:
        response = SESSION.get(
            image_url,
            headers=headers_imagen,
            timeout=25,
            allow_redirects=True,
            stream=True
        )

        response.raise_for_status()

        contenido = bytearray()

        for chunk in response.iter_content(
            chunk_size=65536
        ):
            if not chunk:
                continue

            contenido.extend(chunk)

            if len(contenido) > MAX_IMAGE_BYTES:
                log.warning(
                    "Imagen demasiado grande: "
                    f"{image_url}"
                )
                return None

        if not contenido:
            return None

        entrada = BytesIO(
            bytes(contenido)
        )

        with Image.open(entrada) as imagen:
            imagen.load()

            imagen = ImageOps.exif_transpose(
                imagen
            )

            if imagen.mode != "RGB":
                imagen = imagen.convert(
                    "RGB"
                )

            salida = BytesIO()

            imagen.save(
                salida,
                format="JPEG",
                quality=90,
                optimize=True
            )

            salida.seek(0)

            log.info(
                "Imagen descargada y convertida: "
                f"{image_url}"
            )

            return salida

    except Exception as error:
        log.warning(
            "No se pudo descargar/convertir imagen "
            f"{image_url}: {error}"
        )

        return None


# ============================================================
# METADATOS DEL ARTÍCULO
# ============================================================

def obtener_metadatos_articulo(
    noticia
):
    try:
        response = SESSION.get(
            noticia["link"],
            timeout=20,
            allow_redirects=True
        )

        response.raise_for_status()

        noticia["link"] = response.url

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        noticia["fecha"] = (
            extraer_fecha_desde_soup(
                soup
            )
        )

        noticia["imagen"] = (
            extraer_imagen_desde_soup(
                soup,
                response.url
            )
        )

        if noticia["imagen"]:
            log.info(
                "Imagen detectada para "
                f"'{noticia['titulo']}': "
                f"{noticia['imagen']}"
            )
        else:
            log.warning(
                "No se detectó imagen para: "
                f"{noticia['titulo']}"
            )

        return True

    except requests.exceptions.RequestException as error:
        log.warning(
            "No se pudo abrir artículo "
            f"{noticia['link']}: {error}"
        )

        noticia["fecha"] = None
        noticia["imagen"] = None

        return False


# ============================================================
# DEDUPLICACIÓN
# ============================================================

def eliminar_duplicados(lista):
    unicas = []

    for noticia in lista:
        repetida = False

        for existente in unicas:
            if noticia["link"] == existente["link"]:
                repetida = True
                break

            if titulo_parecido(
                noticia["titulo"],
                existente["titulo"]
            ):
                repetida = True
                break

        if not repetida:
            unicas.append(
                noticia
            )

    return unicas


# ============================================================
# SCRAPING
# ============================================================

def construir_url_absoluta(
    base_url,
    href
):
    if not href:
        return None

    href = href.strip()

    if href.startswith("javascript:"):
        return None

    if href.startswith("#"):
        return None

    return urljoin(
        base_url,
        href
    )


def parece_articulo(link):
    if not link:
        return False

    parsed = urlparse(link)
    path = parsed.path.lower()

    # La Voz de la Frontera
    if "lavozdelafrontera.com.mx" in link:
        if re.search(
            r"-\d{6,}$",
            path.rstrip("/")
        ):
            return True

        return False

    # El Imparcial
    if "elimparcial.com" in link:
        if re.search(
            r"/mxl/mexicali/\d{4}/\d{2}/\d{2}/",
            path
        ):
            return True

        # Compatibilidad con rutas antiguas
        if "/mexicali/" in path and len(path.split("/")) > 4:
            return True

        return False

    # La Crónica
    if "lacronica.com" in link:
        if "/mexicali/" in path:
            return True

        return False

    return False


def obtener_noticias(historial):
    noticias_finales = []

    for orden_fuente, fuente in enumerate(
        FUENTES
    ):
        noticias_fuente = []

        try:
            log.info(
                f"Leyendo: "
                f"{fuente['nombre']}"
            )

            response = SESSION.get(
                fuente["url"],
                timeout=20,
                allow_redirects=True
            )

            if response.status_code != 200:
                log.warning(
                    f"{fuente['nombre']} respondió "
                    f"HTTP {response.status_code}"
                )
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            links = soup.find_all(
                "a",
                href=True
            )

            for posicion, item in enumerate(
                links
            ):
                titulo = item.get_text(
                    " ",
                    strip=True
                )

                href = item.get(
                    "href",
                    ""
                ).strip()

                if (
                    not titulo
                    or len(titulo) < 20
                ):
                    continue

                link = construir_url_absoluta(
                    response.url,
                    href
                )

                if not link:
                    continue

                if not parece_articulo(
                    link
                ):
                    continue

                if not es_noticia_mexicali(
                    titulo,
                    link
                ):
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": link,
                    "fuente": fuente["nombre"],
                    "orden_fuente": orden_fuente,
                    "posicion": posicion
                }

                if historial.ya_fue_enviada(
                    noticia
                ):
                    continue

                noticias_fuente.append(
                    noticia
                )

        except requests.exceptions.RequestException as error:
            log.warning(
                "Error de red en "
                f"{fuente['nombre']}: "
                f"{error}"
            )

        except Exception as error:
            log.exception(
                "Error inesperado en "
                f"{fuente['nombre']}: "
                f"{error}"
            )

        noticias_fuente = eliminar_duplicados(
            noticias_fuente
        )

        limite = LIMITE_POR_FUENTE.get(
            fuente["nombre"],
            3
        )

        noticias_fuente = (
            noticias_fuente[:limite]
        )

        for noticia in noticias_fuente:
            obtener_metadatos_articulo(
                noticia
            )

            if not fecha_aceptable(
                noticia.get("fecha")
            ):
                log.info(
                    "Ignorada por fecha: "
                    f"{noticia['titulo']}"
                )
                continue

            noticias_finales.append(
                noticia
            )

            if (
                len(noticias_finales)
                >= MAX_NOTICIAS_POR_CORRIDA
            ):
                return eliminar_duplicados(
                    noticias_finales
                )

            time.sleep(0.3)

    return eliminar_duplicados(
        noticias_finales
    )


# ============================================================
# TELEGRAM
# ============================================================

def validar_respuesta_telegram(
    response
):
    log.info(
        f"Telegram status: "
        f"{response.status_code}"
    )

    try:
        payload = response.json()

    except ValueError:
        log.error(
            "Telegram devolvió respuesta no JSON: "
            f"{response.text[:500]}"
        )
        return False

    if (
        response.status_code != 200
        or not payload.get(
            "ok",
            False
        )
    ):
        log.error(
            "Telegram rechazó el mensaje: "
            f"{payload}"
        )
        return False

    return True


def enviar_mensaje(
    texto,
    mostrar_preview=False
):
    url = (
        "https://api.telegram.org/"
        f"bot{TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview":
                    not mostrar_preview
            },
            timeout=30
        )

        return validar_respuesta_telegram(
            response
        )

    except requests.exceptions.RequestException as error:
        log.error(
            f"Error enviando mensaje: {error}"
        )

        return False


def enviar_foto_subida(
    noticia,
    imagen_jpeg
):
    titulo = escapar_html(
        noticia["titulo"]
    )

    fuente = escapar_html(
        noticia["fuente"]
    )

    link = escapar_html(
        noticia["link"]
    )

    caption = (
        f"<b>{titulo}</b>\n"
        f"Fuente: {fuente}\n"
        f'<a href="{link}">Abrir noticia</a>'
    )

    url = (
        "https://api.telegram.org/"
        f"bot{TOKEN}/sendPhoto"
    )

    try:
        imagen_jpeg.seek(0)

        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML"
            },
            files={
                "photo": (
                    "noticia.jpg",
                    imagen_jpeg,
                    "image/jpeg"
                )
            },
            timeout=60
        )

        if validar_respuesta_telegram(
            response
        ):
            log.info(
                "Foto subida correctamente para: "
                f"{noticia['titulo']}"
            )

            return True

        return False

    except requests.exceptions.RequestException as error:
        log.error(
            "Error subiendo foto a Telegram: "
            f"{error}"
        )

        return False


def enviar_noticia(noticia):
    """
    Estrategia:
      1) Detecta la imagen del artículo.
      2) La descarga el bot.
      3) La convierte a JPEG.
      4) La sube directamente a Telegram.
      5) Si falla, usa preview normal.
    """

    image_url = noticia.get(
        "imagen"
    )

    if image_url:
        imagen_jpeg = descargar_imagen(
            image_url,
            noticia["link"]
        )

        if imagen_jpeg:
            if enviar_foto_subida(
                noticia,
                imagen_jpeg
            ):
                return True

    log.warning(
        "Usando fallback de preview para: "
        f"{noticia['titulo']}"
    )

    titulo = escapar_html(
        noticia["titulo"]
    )

    fuente = escapar_html(
        noticia["fuente"]
    )

    link = escapar_html(
        noticia["link"]
    )

    mensaje = (
        f"<b>{titulo}</b>\n"
        f"Fuente: {fuente}\n"
        f"Link: {link}"
    )

    return enviar_mensaje(
        mensaje,
        mostrar_preview=True
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        log.error(
            "Falta configurar TOKEN."
        )
        return

    if not CHAT_ID:
        log.error(
            "Falta configurar CHAT_ID."
        )
        return

    log.info(
        "Buscando noticias nuevas de Mexicali..."
    )

    historial = Historial()

    # Registrar ejecución aunque no haya noticias.
    historial.guardar(
        encontrados=0,
        enviados=0
    )

    noticias_a_enviar = obtener_noticias(
        historial
    )

    historial.guardar(
        encontrados=len(
            noticias_a_enviar
        ),
        enviados=0
    )

    if not noticias_a_enviar:
        log.info(
            "No hay noticias nuevas para publicar."
        )
        return

    # ========================================================
    # ENCABEZADO
    # ========================================================

    ahora = (
        datetime
        .now(TZ)
        .strftime("%d/%m/%Y")
    )

    encabezado = (
        "<b>MEXICALI NOTICIAS</b>\n"
        f"<b>Fecha:</b> {ahora}"
    )

    enviar_mensaje(
        encabezado,
        mostrar_preview=False
    )

    time.sleep(2)

    # ========================================================
    # NOTICIAS
    # ========================================================

    total_enviadas = 0
    total_fallidas = 0

    for noticia in noticias_a_enviar:

        enviado = enviar_noticia(
            noticia
        )

        if enviado:
            historial.registrar(
                noticia
            )

            total_enviadas += 1

            historial.guardar(
                encontrados=len(
                    noticias_a_enviar
                ),
                enviados=total_enviadas
            )

        else:
            total_fallidas += 1

            log.warning(
                "No se pudo enviar; "
                "se reintentará en próxima corrida: "
                f"{noticia['titulo']}"
            )

        time.sleep(1)

    historial.guardar(
        encontrados=len(
            noticias_a_enviar
        ),
        enviados=total_enviadas
    )

    log.info(
        f"Total enviadas: "
        f"{total_enviadas} | "
        f"Total fallidas: "
        f"{total_fallidas}"
    )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":
    main()
