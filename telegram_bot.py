import requests
from bs4 import BeautifulSoup
from datetime import datetime
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

TZ = ZoneInfo("America/Tijuana")

# Número de noticias por corrida
MAX_NOTICIAS_POR_CORRIDA = 10

# Historial
MAX_HISTORIAL = 1500

# Similitud para detectar títulos repetidos
UMBRAL_SIMILITUD_TITULO = 0.80

# Máximo tamaño de imagen
MAX_IMAGE_BYTES = 18 * 1024 * 1024


# ============================================================
# FUENTES
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
        "nombre": "La Voz de la Frontera - Deportes",
        "url": "https://www.lavozdelafrontera.com.mx/deportes/"
    },

    {
        "nombre": "El Imparcial Mexicali",
        "url": "https://www.elimparcial.com/mexicali/"
    },

    {
        "nombre": "El Imparcial Policiaca",
        "url": "https://www.elimparcial.com/mxl/policiaca/"
    },

    {
        "nombre": "La Crónica Mexicali",
        "url": "https://www.lacronica.com/mexicali/"
    },

    {
        "nombre": "La Crónica Policiaca",
        "url": "https://www.lacronica.com/mxl/policiaca/"
    }
]


# ============================================================
# HEADERS
# ============================================================

HEADERS = {

    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),

    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),

    "Accept-Language":
        "es-MX,es;q=0.9,en;q=0.8",

    "Cache-Control":
        "no-cache",

    "Pragma":
        "no-cache"
}


SESSION = requests.Session()

SESSION.headers.update(
    HEADERS
)


# ============================================================
# UTILIDADES
# ============================================================

def limpiar_texto(texto):

    texto = str(
        texto or ""
    ).lower()

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

        texto = texto.replace(
            original,
            reemplazo
        )

    texto = re.sub(
        r"[^a-z0-9\s]",
        " ",
        texto
    )

    texto = re.sub(
        r"\s+",
        " ",
        texto
    ).strip()

    return texto


def escapar_html(texto):

    texto = str(
        texto or ""
    )

    return (
        texto
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def titulo_parecido(
    titulo1,
    titulo2
):

    a = limpiar_texto(
        titulo1
    )

    b = limpiar_texto(
        titulo2
    )

    if not a or not b:

        return False

    similitud = SequenceMatcher(
        None,
        a,
        b
    ).ratio()

    return (
        similitud
        >= UMBRAL_SIMILITUD_TITULO
    )


# ============================================================
# HISTORIAL
# ============================================================

def historial_vacio():

    return {

        "ultima_ejecucion":
            None,

        "ultimo_total_encontrado":
            0,

        "ultimo_total_enviado":
            0,

        "links":
            [],

        "titulos":
            []
    }


def cargar_historial():

    if not os.path.exists(
        ARCHIVO_ENVIADAS
    ):

        return historial_vacio()

    try:

        with open(
            ARCHIVO_ENVIADAS,
            "r",
            encoding="utf-8"
        ) as archivo:

            data = json.load(
                archivo
            )

        if not isinstance(
            data,
            dict
        ):

            return historial_vacio()

        base = historial_vacio()

        base.update(
            data
        )

        if not isinstance(
            base["links"],
            list
        ):

            base["links"] = []

        if not isinstance(
            base["titulos"],
            list
        ):

            base["titulos"] = []

        return base

    except Exception as error:

        log.error(
            f"Error leyendo historial: "
            f"{error}"
        )

        try:

            backup = (
                ARCHIVO_ENVIADAS
                + ".backup_"
                + str(
                    int(
                        time.time()
                    )
                )
            )

            os.replace(
                ARCHIVO_ENVIADAS,
                backup
            )

        except OSError:

            pass

        return historial_vacio()


def guardar_historial_disco(
    historial
):

    historial["links"] = (
        historial
        .get(
            "links",
            []
        )
        [-MAX_HISTORIAL:]
    )

    historial["titulos"] = (
        historial
        .get(
            "titulos",
            []
        )
        [-MAX_HISTORIAL:]
    )

    temporal = (
        ARCHIVO_ENVIADAS
        + ".tmp"
    )

    with open(
        temporal,
        "w",
        encoding="utf-8"
    ) as archivo:

        json.dump(
            historial,
            archivo,
            ensure_ascii=False,
            indent=2
        )

        archivo.write(
            "\n"
        )

    os.replace(
        temporal,
        ARCHIVO_ENVIADAS
    )


class Historial:

    def __init__(self):

        data = cargar_historial()

        self.links = set(
            data["links"]
        )

        self.titulos = list(
            data["titulos"]
        )

        self.ultima_ejecucion = (
            data.get(
                "ultima_ejecucion"
            )
        )

        self.ultimo_total_encontrado = (
            data.get(
                "ultimo_total_encontrado",
                0
            )
        )

        self.ultimo_total_enviado = (
            data.get(
                "ultimo_total_enviado",
                0
            )
        )


    def ya_fue_enviada(
        self,
        noticia
    ):

        if (
            noticia["link"]
            in self.links
        ):

            return True

        for titulo_guardado in self.titulos:

            if titulo_parecido(
                noticia["titulo"],
                titulo_guardado
            ):

                return True

        return False


    def registrar(
        self,
        noticia
    ):

        self.links.add(
            noticia["link"]
        )

        if (
            noticia["titulo"]
            not in self.titulos
        ):

            self.titulos.append(
                noticia["titulo"]
            )


    def guardar(
        self,
        encontrados=None,
        enviados=None
    ):

        if encontrados is not None:

            self.ultimo_total_encontrado = (
                encontrados
            )

        if enviados is not None:

            self.ultimo_total_enviado = (
                enviados
            )

        self.ultima_ejecucion = (
            datetime
            .now(TZ)
            .isoformat()
        )

        guardar_historial_disco({

            "ultima_ejecucion":
                self.ultima_ejecucion,

            "ultimo_total_encontrado":
                self.ultimo_total_encontrado,

            "ultimo_total_enviado":
                self.ultimo_total_enviado,

            "links":
                list(
                    self.links
                ),

            "titulos":
                self.titulos
        })


# ============================================================
# FILTRO MEXICALI
# ============================================================

def es_noticia_mexicali(
    titulo,
    link
):

    texto = limpiar_texto(
        titulo
        + " "
        + link
    )

    claves = [

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


    ciudades_excluidas = [

        "tijuana",

        "ensenada",

        "rosarito",

        "tecate",

        "san felipe",

        "san quintin",

        "san luis rio colorado",

        "slrc",

        "hermosillo"
    ]


    for ciudad in ciudades_excluidas:

        if (
            ciudad in texto
            and
            "mexicali"
            not in texto
        ):

            return False


    return any(

        clave in texto

        for clave in claves
    )


# ============================================================
# FECHAS
# ============================================================

def convertir_fecha(
    fecha_texto
):

    if not fecha_texto:

        return None

    fecha_texto = str(
        fecha_texto
    ).strip()

    try:

        if fecha_texto.endswith(
            "Z"
        ):

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

        return fecha.astimezone(
            TZ
        )

    except Exception:

        return None


def obtener_fecha_url(
    url
):

    match = re.search(
        r"/(\d{4})/(\d{2})/(\d{2})/",
        url
    )

    if not match:

        return None

    try:

        return datetime(

            int(
                match.group(1)
            ),

            int(
                match.group(2)
            ),

            int(
                match.group(3)
            ),

            tzinfo=TZ
        )

    except ValueError:

        return None


def obtener_fecha_soup(
    soup
):

    metas = [

        {
            "property":
                "article:published_time"
        },

        {
            "property":
                "article:modified_time"
        },

        {
            "name":
                "date"
        },

        {
            "name":
                "pubdate"
        },

        {
            "itemprop":
                "datePublished"
        }
    ]


    for meta_info in metas:

        meta = soup.find(
            "meta",
            attrs=meta_info
        )

        if (
            meta
            and
            meta.get(
                "content"
            )
        ):

            fecha = convertir_fecha(
                meta.get(
                    "content"
                )
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

        match = re.search(

            r'"datePublished"\s*:\s*"([^"]+)"',

            texto
        )

        if match:

            fecha = convertir_fecha(
                match.group(1)
            )

            if fecha:

                return fecha


    return None


# ============================================================
# IMAGEN
# ============================================================

def meta_imagen(
    soup,
    tipo,
    nombre
):

    meta = soup.find(
        "meta",
        attrs={
            tipo:
                nombre
        }
    )

    if (
        meta
        and
        meta.get(
            "content"
        )
    ):

        return meta[
            "content"
        ].strip()

    return None


def imagen_json_ld(
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

                imagen = (
                    match.group(1)
                    .replace(
                        "\\/",
                        "/"
                    )
                )

                return urljoin(
                    article_url,
                    imagen
                )


    return None


def imagen_valida(
    url
):

    if not url:

        return False

    texto = limpiar_texto(
        url
    )

    excluir = [

        "logo",

        "favicon",

        "icon",

        "avatar",

        "placeholder",

        "sprite",

        "tracking",

        "pixel",

        "banner"
    ]

    return not any(

        item in texto

        for item in excluir
    )


def obtener_imagen_articulo(
    soup,
    article_url
):

    candidatos = [

        meta_imagen(
            soup,
            "property",
            "og:image"
        ),

        meta_imagen(
            soup,
            "property",
            "og:image:secure_url"
        ),

        meta_imagen(
            soup,
            "name",
            "twitter:image"
        ),

        meta_imagen(
            soup,
            "name",
            "twitter:image:src"
        )
    ]


    for candidato in candidatos:

        if candidato:

            imagen = urljoin(
                article_url,
                candidato
            )

            if imagen_valida(
                imagen
            ):

                return imagen


    imagen = imagen_json_ld(
        soup,
        article_url
    )

    if imagen_valida(
        imagen
    ):

        return imagen


    article = soup.find(
        "article"
    )


    contenedores = [

        article,

        soup.find(
            "main"
        ),

        soup
    ]


    for contenedor in contenedores:

        if not contenedor:

            continue


        for img in contenedor.find_all(
            "img"
        ):

            atributos = [

                "data-src",

                "data-lazy-src",

                "data-original",

                "src"
            ]


            for atributo in atributos:

                valor = img.get(
                    atributo
                )

                if not valor:

                    continue


                imagen = urljoin(
                    article_url,
                    valor
                )


                if imagen_valida(
                    imagen
                ):

                    return imagen


    return None


# ============================================================
# DESCARGAR IMAGEN
# ============================================================

def descargar_imagen(
    image_url,
    article_url
):

    if not image_url:

        return None


    headers_imagen = {

        "User-Agent":
            HEADERS["User-Agent"],

        "Accept":
            "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",

        "Referer":
            article_url
    }


    try:

        response = SESSION.get(

            image_url,

            headers=
                headers_imagen,

            timeout=25,

            stream=True,

            allow_redirects=True
        )


        response.raise_for_status()


        contenido = bytearray()


        for chunk in response.iter_content(
            chunk_size=65536
        ):

            if not chunk:

                continue


            contenido.extend(
                chunk
            )


            if (
                len(
                    contenido
                )
                >
                MAX_IMAGE_BYTES
            ):

                return None


        entrada = BytesIO(
            bytes(
                contenido
            )
        )


        with Image.open(
            entrada
        ) as imagen:

            imagen.load()


            imagen = (
                ImageOps
                .exif_transpose(
                    imagen
                )
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


            salida.seek(
                0
            )


            return salida


    except Exception as error:

        log.warning(
            f"Error imagen: "
            f"{error}"
        )

        return None


# ============================================================
# METADATOS ARTÍCULO
# ============================================================

def obtener_metadatos(
    noticia
):

    try:

        response = SESSION.get(

            noticia["link"],

            timeout=20,

            allow_redirects=True
        )


        response.raise_for_status()


        noticia["link"] = (
            response.url
        )


        soup = BeautifulSoup(

            response.text,

            "html.parser"
        )


        fecha = obtener_fecha_soup(
            soup
        )


        if not fecha:

            fecha = obtener_fecha_url(
                response.url
            )


        noticia["fecha"] = (
            fecha
        )


        noticia["imagen"] = (
            obtener_imagen_articulo(

                soup,

                response.url
            )
        )


        return True


    except Exception as error:

        log.warning(
            "No se pudo leer artículo: "
            f"{error}"
        )


        noticia["fecha"] = (
            obtener_fecha_url(
                noticia["link"]
            )
        )


        noticia["imagen"] = None


        return False


# ============================================================
# DUPLICADOS
# ============================================================

def eliminar_duplicados(
    noticias
):

    resultado = []


    for noticia in noticias:

        repetida = False


        for existente in resultado:


            if (
                noticia["link"]
                ==
                existente["link"]
            ):

                repetida = True

                break


            if titulo_parecido(

                noticia["titulo"],

                existente["titulo"]
            ):

                repetida = True

                break


        if not repetida:

            resultado.append(
                noticia
            )


    return resultado


# ============================================================
# DETECTAR LINKS DE ARTÍCULOS
# ============================================================

def parece_articulo(
    link
):

    if not link:

        return False


    path = (
        urlparse(
            link
        )
        .path
        .lower()
    )


    if (
        "lavozdelafrontera.com.mx"
        in link
    ):

        return bool(

            re.search(

                r"-\d{6,}$",

                path.rstrip(
                    "/"
                )
            )
        )


    if (
        "elimparcial.com"
        in link
    ):

        return bool(

            re.search(

                r"/mxl/(mexicali|policiaca)/\d{4}/\d{2}/\d{2}/",

                path
            )
        )


    if (
        "lacronica.com"
        in link
    ):

        return bool(

            "/mexicali/"
            in path

            or

            "/policiaca/"
            in path
        )


    return False


# ============================================================
# OBTENER NOTICIAS
# ============================================================

def obtener_noticias(
    historial
):

    candidatas = []


    # --------------------------------------------------------
    # REVISAR TODAS LAS FUENTES
    # --------------------------------------------------------

    for fuente in FUENTES:

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


            if (
                response.status_code
                != 200
            ):

                continue


            soup = BeautifulSoup(

                response.text,

                "html.parser"
            )


            links = soup.find_all(

                "a",

                href=True
            )


            for item in links:


                titulo = item.get_text(

                    " ",

                    strip=True
                )


                href = item.get(
                    "href",
                    ""
                )


                if (
                    not titulo
                    or
                    len(titulo) < 20
                ):

                    continue


                link = urljoin(

                    response.url,

                    href
                )


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

                    "titulo":
                        titulo,

                    "link":
                        link,

                    "fuente":
                        fuente["nombre"],

                    "fecha":
                        obtener_fecha_url(
                            link
                        )
                }


                if historial.ya_fue_enviada(
                    noticia
                ):

                    continue


                candidatas.append(
                    noticia
                )


        except Exception as error:

            log.warning(

                f"Error leyendo "
                f"{fuente['nombre']}: "
                f"{error}"
            )


    # --------------------------------------------------------
    # ELIMINAR DUPLICADOS
    # --------------------------------------------------------

    candidatas = eliminar_duplicados(
        candidatas
    )


    log.info(

        f"Candidatas encontradas: "
        f"{len(candidatas)}"
    )


    # --------------------------------------------------------
    # OBTENER FECHA REAL
    # --------------------------------------------------------

    for noticia in candidatas:

        obtener_metadatos(
            noticia
        )

        time.sleep(
            0.15
        )


    # --------------------------------------------------------
    # ORDENAR DE MÁS NUEVA A MÁS ANTIGUA
    # --------------------------------------------------------

    fecha_minima = (
        datetime.min
        .replace(
            tzinfo=TZ
        )
    )


    candidatas.sort(

        key=lambda noticia:

            noticia.get(
                "fecha"
            )
            or
            fecha_minima,

        reverse=True
    )


    # --------------------------------------------------------
    # SELECCIONAR 10
    # --------------------------------------------------------

    seleccionadas = (
        candidatas[
            :MAX_NOTICIAS_POR_CORRIDA
        ]
    )


    log.info(

        f"Noticias seleccionadas: "
        f"{len(seleccionadas)}"
    )


    return seleccionadas


# ============================================================
# TELEGRAM
# ============================================================

def validar_telegram(
    response
):

    try:

        payload = response.json()

    except ValueError:

        log.error(
            response.text
        )

        return False


    if (
        response.status_code
        != 200
        or
        not payload.get(
            "ok",
            False
        )
    ):

        log.error(
            payload
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

                "chat_id":
                    CHAT_ID,

                "text":
                    texto,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    not mostrar_preview
            },

            timeout=30
        )


        return validar_telegram(
            response
        )


    except Exception as error:

        log.error(
            f"Telegram: "
            f"{error}"
        )

        return False


# ============================================================
# ENVIAR FOTO
# ============================================================

def enviar_foto(
    noticia,
    imagen
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

        f'<a href="{link}">'
        f'Abrir noticia'
        f'</a>'
    )


    url = (

        "https://api.telegram.org/"

        f"bot{TOKEN}/sendPhoto"
    )


    try:

        imagen.seek(
            0
        )


        response = requests.post(

            url,

            data={

                "chat_id":
                    CHAT_ID,

                "caption":
                    caption,

                "parse_mode":
                    "HTML"
            },

            files={

                "photo": (

                    "noticia.jpg",

                    imagen,

                    "image/jpeg"
                )
            },

            timeout=60
        )


        return validar_telegram(
            response
        )


    except Exception as error:

        log.error(
            f"Error sendPhoto: "
            f"{error}"
        )

        return False


# ============================================================
# ENVIAR NOTICIA
# ============================================================

def enviar_noticia(
    noticia
):

    # --------------------------------------------------------
    # INTENTAR FOTO
    # --------------------------------------------------------

    if noticia.get(
        "imagen"
    ):

        imagen = descargar_imagen(

            noticia["imagen"],

            noticia["link"]
        )


        if imagen:

            if enviar_foto(

                noticia,

                imagen
            ):

                return True


    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

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
            "Falta TOKEN"
        )

        return


    if not CHAT_ID:

        log.error(
            "Falta CHAT_ID"
        )

        return


    log.info(
        "Buscando 10 noticias de Mexicali..."
    )


    historial = Historial()


    historial.guardar(

        encontrados=0,

        enviados=0
    )


    noticias = obtener_noticias(
        historial
    )


    historial.guardar(

        encontrados=
            len(noticias),

        enviados=0
    )


    if not noticias:

        log.info(
            "No hay noticias nuevas."
        )

        return


    # ========================================================
    # ENCABEZADO
    # ========================================================

    fecha = (

        datetime
        .now(TZ)
        .strftime(
            "%d/%m/%Y"
        )
    )


    encabezado = (

        "<b>MEXICALI NOTICIAS</b>\n"

        f"<b>Fecha:</b> {fecha}\n"

        f"<b>Noticias:</b> "
        f"{len(noticias)}"
    )


    enviar_mensaje(

        encabezado,

        mostrar_preview=False
    )


    time.sleep(
        2
    )


    # ========================================================
    # ENVIAR 10
    # ========================================================

    enviadas = 0

    fallidas = 0


    for noticia in noticias:


        resultado = enviar_noticia(
            noticia
        )


        if resultado:


            historial.registrar(
                noticia
            )


            enviadas += 1


            historial.guardar(

                encontrados=
                    len(noticias),

                enviados=
                    enviadas
            )


        else:


            fallidas += 1


        time.sleep(
            1
        )


    # ========================================================
    # FINAL
    # ========================================================

    historial.guardar(

        encontrados=
            len(noticias),

        enviados=
            enviadas
    )


    log.info(

        f"Encontradas: "
        f"{len(noticias)} | "

        f"Enviadas: "
        f"{enviadas} | "

        f"Fallidas: "
        f"{fallidas}"
    )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":

    main()
