import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import time
import json
import os
import re
import logging
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

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

UMBRAL_SIMILITUD_TITULO = 0.80
MAX_HISTORIAL = 1000

FUENTES = [
    {"nombre": "La Voz de la Frontera", "url": "https://www.lavozdelafrontera.com.mx/local/"},
    {"nombre": "El Imparcial Mexicali", "url": "https://www.elimparcial.com/mexicali/"},
    {"nombre": "La Crónica Mexicali", "url": "https://www.lacronica.com/mexicali/"}
]

LIMITE_POR_FUENTE = {
    "La Voz de la Frontera": 4,
    "El Imparcial Mexicali": 3,
    "La Crónica Mexicali": 3
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def limpiar_texto(texto):
    texto = texto.lower()
    texto = texto.replace("á", "a").replace("é", "e")
    texto = texto.replace("í", "i").replace("ó", "o")
    texto = texto.replace("ú", "u").replace("ñ", "n")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def escapar_html(texto):
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def titulo_parecido(t1, t2):
    return SequenceMatcher(None, limpiar_texto(t1), limpiar_texto(t2)).ratio() >= UMBRAL_SIMILITUD_TITULO


# ---------------------------------------------------------------------------
# Historial (cargado UNA sola vez en memoria por corrida)
# ---------------------------------------------------------------------------

def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return {"links": [], "titulos": []}

    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"links": [], "titulos": []}
            data.setdefault("links", [])
            data.setdefault("titulos", [])
            return data

    except Exception as error:
        log.error(f"Error leyendo historial, se respalda y reinicia: {error}")
        try:
            os.replace(ARCHIVO_ENVIADAS, f"{ARCHIVO_ENVIADAS}.bak_{int(time.time())}")
        except OSError:
            pass
        return {"links": [], "titulos": []}


def guardar_enviadas_en_disco(historial):
    historial["links"] = historial["links"][-MAX_HISTORIAL:]
    historial["titulos"] = historial["titulos"][-MAX_HISTORIAL:]

    with open(ARCHIVO_ENVIADAS, "w", encoding="utf-8") as f:
        json.dump(historial, f, ensure_ascii=False, indent=2)


class Historial:
    """Envoltorio en memoria del historial de noticias enviadas.
    Evita releer/reparsear el JSON en cada verificación, que era el
    cuello de botella del script original (una lectura de disco +
    fuzzy-matching contra hasta 1000 títulos por CADA link candidato)."""

    def __init__(self):
        data = cargar_enviadas()
        self.links = set(data["links"])
        self.titulos = list(data["titulos"])
        self._hay_cambios = False

    def ya_fue_enviada(self, noticia):
        if noticia["link"] in self.links:
            return True

        return any(
            titulo_parecido(noticia["titulo"], titulo_guardado)
            for titulo_guardado in self.titulos
        )

    def registrar(self, noticia):
        if noticia["link"] not in self.links:
            self.links.add(noticia["link"])
            self._hay_cambios = True

        if noticia["titulo"] not in self.titulos:
            self.titulos.append(noticia["titulo"])
            self._hay_cambios = True

    def persistir_si_hay_cambios(self):
        if not self._hay_cambios:
            return

        guardar_enviadas_en_disco({
            "links": list(self.links),
            "titulos": self.titulos
        })
        self._hay_cambios = False
        log.info(f"Historial guardado: {len(self.links)} links, {len(self.titulos)} títulos")


# ---------------------------------------------------------------------------
# Filtro geográfico
# ---------------------------------------------------------------------------

def es_noticia_mexicali(titulo, link):
    texto = limpiar_texto(titulo + " " + link)

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

    return any(clave in texto for clave in claves_mexicali)


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------

def convertir_fecha(fecha_texto):
    if not fecha_texto:
        return None

    try:
        fecha_texto = fecha_texto.strip()

        if fecha_texto.endswith("Z"):
            fecha_texto = fecha_texto.replace("Z", "+00:00")

        fecha = datetime.fromisoformat(fecha_texto)

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=TZ)

        return fecha.astimezone(TZ)

    except (ValueError, TypeError):
        return None


def obtener_fecha_articulo(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

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
            meta = soup.find("meta", meta_info)

            if meta and meta.get("content"):
                fecha = convertir_fecha(meta.get("content"))
                if fecha:
                    return fecha

        scripts = soup.find_all("script", type="application/ld+json")

        for script in scripts:
            texto = script.get_text(" ", strip=True)
            coincidencias = re.findall(r'"datePublished"\s*:\s*"([^"]+)"', texto)

            for fecha_texto in coincidencias:
                fecha = convertir_fecha(fecha_texto)
                if fecha:
                    return fecha

        return None

    except requests.exceptions.RequestException as error:
        log.warning(f"No se pudo obtener fecha de {link}: {error}")
        return None


def es_hoy_o_ayer(noticia):
    fecha = obtener_fecha_articulo(noticia["link"])

    hoy = datetime.now(TZ).date()
    ayer = hoy - timedelta(days=1)

    if fecha:
        noticia["fecha"] = fecha
        return fecha.date() in [hoy, ayer]

    # Sin fecha detectable: se incluye por defecto (comportamiento original)
    return True


# ---------------------------------------------------------------------------
# Deduplicación
# ---------------------------------------------------------------------------

def eliminar_duplicados(lista):
    unicas = []

    for noticia in lista:
        repetida = False

        for existente in unicas:
            if noticia["link"] == existente["link"]:
                repetida = True
                break

            if titulo_parecido(noticia["titulo"], existente["titulo"]):
                repetida = True
                break

        if not repetida:
            unicas.append(noticia)

    return unicas


# ---------------------------------------------------------------------------
# Scraping de fuentes
# ---------------------------------------------------------------------------

def construir_url_absoluta(base_url, href):
    if href.startswith("http"):
        return href

    if href.startswith("/"):
        partes = urlparse(base_url)
        return f"{partes.scheme}://{partes.netloc}{href}"

    return None


def obtener_noticias(historial):
    noticias_finales = []

    for orden_fuente, fuente in enumerate(FUENTES):
        noticias_fuente = []

        try:
            log.info(f"Leyendo: {fuente['nombre']}")

            r = requests.get(fuente["url"], headers=HEADERS, timeout=10)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.find_all("a", href=True)

            for posicion, item in enumerate(links):
                titulo = item.get_text(" ", strip=True)
                href = item["href"]

                if not titulo or len(titulo) < 30:
                    continue

                href = construir_url_absoluta(fuente["url"], href)

                if not href:
                    continue

                if not es_noticia_mexicali(titulo, href):
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": href,
                    "fuente": fuente["nombre"],
                    "orden_fuente": orden_fuente,
                    "posicion": posicion
                }

                if historial.ya_fue_enviada(noticia):
                    log.info(f"Repetida, se omite: {titulo}")
                    continue

                if not es_hoy_o_ayer(noticia):
                    continue

                noticias_fuente.append(noticia)

        except requests.exceptions.RequestException as e:
            log.warning(f"Error de red en {fuente['nombre']}: {e}")
        except Exception as e:
            log.error(f"Error inesperado en {fuente['nombre']}: {e}")

        noticias_fuente = eliminar_duplicados(noticias_fuente)

        limite = LIMITE_POR_FUENTE.get(fuente["nombre"], 3)
        noticias_finales.extend(noticias_fuente[:limite])

    return eliminar_duplicados(noticias_finales)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def enviar_mensaje(texto):
    """Envía un mensaje a Telegram. Retorna True solo si Telegram confirma
    la entrega (HTTP 200 + ok:true en el payload)."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            },
            timeout=20
        )

        log.info(f"Telegram status: {response.status_code}")

        if response.status_code != 200:
            log.error(f"Telegram respondió con error: {response.text}")
            return False

        payload = response.json()
        if not payload.get("ok", False):
            log.error(f"Telegram ok=false: {payload}")
            return False

        return True

    except requests.exceptions.RequestException as error:
        log.error(f"Excepción enviando a Telegram: {error}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        log.error("Falta configurar TOKEN.")
        return

    if not CHAT_ID:
        log.error("Falta configurar CHAT_ID.")
        return

    log.info("Buscando noticias nuevas de Mexicali...")

    historial = Historial()
    noticias_a_enviar = obtener_noticias(historial)

    if not noticias_a_enviar:
        log.info("No hay noticias nuevas para publicar.")
        return

    ahora = datetime.now(TZ).strftime("%d/%m/%Y")

    encabezado = (
        f"<b>MEXICALI NOTICIAS</b>\n"
        f"<b>Fecha:</b> {ahora}"
    )

    enviar_mensaje(encabezado)
    time.sleep(2)

    total_enviadas = 0
    total_fallidas = 0

    for noticia in noticias_a_enviar:
        titulo = escapar_html(noticia["titulo"])
        fuente = escapar_html(noticia["fuente"])
        link = escapar_html(noticia["link"])

        mensaje = (
            f"<b>{titulo}</b>\n"
            f"Fuente: {fuente}\n"
            f"Link: {link}"
        )

        enviado = enviar_mensaje(mensaje)

        if enviado:
            historial.registrar(noticia)
            total_enviadas += 1
        else:
            total_fallidas += 1
            log.warning(f"No se pudo enviar (se reintentará en próxima corrida): {noticia['titulo']}")

        time.sleep(1)

    historial.persistir_si_hay_cambios()

    log.info(f"Total enviadas: {total_enviadas} | Total fallidas: {total_fallidas}")


if __name__ == "__main__":
    main()
