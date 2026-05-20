import requests
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import json
import os
import re
from difflib import SequenceMatcher

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas_mexicali.json"
TZ = ZoneInfo("America/Tijuana")

FUENTES = [
    {"nombre": "La Voz de la Frontera", "url": "https://www.lavozdelafrontera.com.mx/local/"},
    {"nombre": "El Imparcial Mexicali", "url": "https://www.elimparcial.com/mexicali/"},
    {"nombre": "La Crónica Mexicali", "url": "https://www.lacronica.com/mexicali/"}
]

HEADERS = {"User-Agent": "Mozilla/5.0"}


def limpiar_texto(texto):
    texto = texto.lower()
    texto = texto.replace("á", "a").replace("é", "e")
    texto = texto.replace("í", "i").replace("ó", "o")
    texto = texto.replace("ú", "u").replace("ñ", "n")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def escapar_html(texto):
    return (
        texto.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def titulo_parecido(t1, t2):
    return SequenceMatcher(None, limpiar_texto(t1), limpiar_texto(t2)).ratio() >= 0.80


def cargar_enviadas():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return {"links": [], "titulos": []}

    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"links": [], "titulos": []}


def guardar_enviada(noticia):
    data = cargar_enviadas()

    if noticia["link"] not in data["links"]:
        data["links"].append(noticia["link"])

    if noticia["titulo"] not in data["titulos"]:
        data["titulos"].append(noticia["titulo"])

    data["links"] = data["links"][-300:]
    data["titulos"] = data["titulos"][-300:]

    with open(ARCHIVO_ENVIADAS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ya_fue_enviada(noticia):
    data = cargar_enviadas()

    if noticia["link"] in data["links"]:
        return True

    for titulo_guardado in data["titulos"]:
        if titulo_parecido(noticia["titulo"], titulo_guardado):
            return True

    return False


def es_noticia_mexicali(titulo, link):
    texto = limpiar_texto(titulo + " " + link)

    claves = [
        "mexicali",
        "valle de mexicali",
        "calexico",
        "garita",
        "aduana",
        "frontera",
        "baja california",
        "bc",
        "cachanilla",
        "palaco",
        "pueblo nuevo",
        "zona centro",
        "progreso",
        "policia",
        "bomberos",
        "fge"
    ]

    return any(c in texto for c in claves)


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

    except:
        return None


def obtener_fecha_articulo(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        posibles_metas = [
            {"property": "article:published_time"},
            {"property": "article:modified_time"},
            {"name": "date"},
            {"name": "pubdate"},
            {"name": "publishdate"},
            {"name": "timestamp"},
            {"itemprop": "datePublished"},
            {"itemprop": "dateModified"}
        ]

        for meta_info in posibles_metas:
            meta = soup.find("meta", meta_info)

            if meta and meta.get("content"):
                fecha = convertir_fecha(meta.get("content"))

                if fecha:
                    return fecha

        scripts = soup.find_all("script", type="application/ld+json")

        for script in scripts:
            texto = script.get_text(" ", strip=True)

            coincidencias = re.findall(
                r'"datePublished"\s*:\s*"([^"]+)"',
                texto
            )

            for fecha_texto in coincidencias:
                fecha = convertir_fecha(fecha_texto)

                if fecha:
                    return fecha

        return None

    except Exception as e:
        print(f"No se pudo obtener fecha del artículo: {link} | {e}")
        return None


def es_de_hoy(noticia):
    fecha_articulo = obtener_fecha_articulo(noticia["link"])

    if not fecha_articulo:
        print(f"SIN FECHA CONFIRMADA, SE OMITE: {noticia['titulo']}")
        return False

    hoy_mexicali = datetime.now(TZ).date()

    if fecha_articulo.date() == hoy_mexicali:
        return True

    print(
        f"NOTICIA VIEJA, SE OMITE: {noticia['titulo']} | "
        f"Fecha artículo: {fecha_articulo.strftime('%d/%m/%Y %I:%M %p')}"
    )

    return False


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


def obtener_noticias():
    noticias = []
    data_enviadas = cargar_enviadas()

    for fuente in FUENTES:
        try:
            print(f"Leyendo: {fuente['nombre']}")

            r = requests.get(fuente["url"], headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.find_all("a", href=True)

            for item in links:
                titulo = item.get_text(" ", strip=True)
                href = item["href"]

                if not titulo or len(titulo) < 30:
                    continue

                if href.startswith("/"):
                    base = fuente["url"].split("/")[0] + "//" + fuente["url"].split("/")[2]
                    href = base + href

                if not href.startswith("http"):
                    continue

                if not es_noticia_mexicali(titulo, href):
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": href,
                    "fuente": fuente["nombre"]
                }

                if noticia["link"] in data_enviadas["links"]:
                    print(f"REPETIDA LINK: {titulo}")
                    continue

                repetida = False

                for titulo_guardado in data_enviadas["titulos"]:
                    if titulo_parecido(titulo, titulo_guardado):
                        repetida = True
                        break

                if repetida:
                    print(f"REPETIDA TITULO: {titulo}")
                    continue

                if not es_de_hoy(noticia):
                    continue

                noticias.append(noticia)

        except Exception as e:
            print(f"Error en {fuente['nombre']}: {e}")

    return eliminar_duplicados(noticias)


def enviar_mensaje(texto):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
    )

    print(response.status_code)
    print(response.text)

    return response.status_code == 200


def main():
    print("Buscando noticias de Mexicali del día actual...")

    noticias = obtener_noticias()

    noticias_nuevas = []

    for noticia in noticias:
        if not ya_fue_enviada(noticia):
            noticias_nuevas.append(noticia)

    noticias_a_enviar = noticias_nuevas[:10]

    if not noticias_a_enviar:
        print("No hay noticias nuevas de hoy. No se publica nada.")
        return

    ahora = datetime.now(TZ).strftime("%d/%m/%Y %I:%M %p")

    encabezado = (
        f"<b>MEXICALI NOTICIAS</b>\n"
        f"<b>Fecha y hora:</b> {ahora}\n"
        f"<b>Cobertura:</b> Noticias de hoy"
    )

    enviar_mensaje(encabezado)

    time.sleep(2)

    for i, noticia in enumerate(noticias_a_enviar, 1):
        titulo = escapar_html(noticia["titulo"])
        fuente = escapar_html(noticia["fuente"])
        link = escapar_html(noticia["link"])

        mensaje = (
            f"<b>{i}. {titulo}</b>\n"
            f"Fuente: {fuente}\n"
            f"Link: {link}"
        )

        enviado = enviar_mensaje(mensaje)

        if enviado:
            guardar_enviada(noticia)

        time.sleep(1)


if __name__ == "__main__":
    main()
