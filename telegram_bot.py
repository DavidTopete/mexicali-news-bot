import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import json
import os
import re
from difflib import SequenceMatcher

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas_mexicali.json"

FUENTES = [
    {"nombre": "La Voz de la Frontera", "url": "https://www.lavozdelafrontera.com.mx/local/"},
    {"nombre": "El Imparcial Mexicali", "url": "https://www.elimparcial.com/mexicali/"},
    {"nombre": "La Crónica Mexicali", "url": "https://www.lacronica.com/mexicali/"}
]

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

# ========================
# UTILIDADES
# ========================

def limpiar_texto(texto):
    texto = texto.lower()
    texto = texto.replace("á", "a").replace("é", "e")
    texto = texto.replace("í", "i").replace("ó", "o")
    texto = texto.replace("ú", "u").replace("ñ", "n")

    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def titulo_parecido(t1, t2):
    return SequenceMatcher(
        None,
        limpiar_texto(t1),
        limpiar_texto(t2)
    ).ratio() >= 0.80


def cargar_enviadas():

    if not os.path.exists(ARCHIVO_ENVIADAS):
        return {
            "links": [],
            "titulos": []
        }

    try:
        with open(
            ARCHIVO_ENVIADAS,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except:
        return {
            "links": [],
            "titulos": []
        }


def guardar_enviada(noticia):

    data = cargar_enviadas()

    if noticia["link"] not in data["links"]:
        data["links"].append(noticia["link"])

    if noticia["titulo"] not in data["titulos"]:
        data["titulos"].append(noticia["titulo"])

    data["links"] = data["links"][-300:]
    data["titulos"] = data["titulos"][-300:]

    with open(
        ARCHIVO_ENVIADAS,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def ya_fue_enviada(noticia):

    data = cargar_enviadas()

    if noticia["link"] in data["links"]:
        return True

    for titulo_guardado in data["titulos"]:

        if titulo_parecido(
            noticia["titulo"],
            titulo_guardado
        ):
            return True

    return False


# ========================
# FILTRO MEXICALI
# ========================

def es_noticia_mexicali(titulo, link):

    texto = limpiar_texto(
        titulo + " " + link
    )

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

    return any(
        c in texto for c in claves
    )


# ========================
# ELIMINAR DUPLICADOS
# ========================

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
            unicas.append(noticia)

    return unicas


# ========================
# SCRAPING
# ========================

def obtener_noticias():

    noticias = []

    data_enviadas = cargar_enviadas()

    for fuente in FUENTES:

        try:

            print(f"Leyendo: {fuente['nombre']}")

            r = requests.get(
                fuente["url"],
                headers=HEADERS,
                timeout=10
            )

            soup = BeautifulSoup(
                r.text,
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

                href = item["href"]

                if not titulo or len(titulo) < 30:
                    continue

                if href.startswith("/"):

                    base = (
                        fuente["url"].split("/")[0]
                        + "//"
                        + fuente["url"].split("/")[2]
                    )

                    href = base + href

                if not href.startswith("http"):
                    continue

                if not es_noticia_mexicali(
                    titulo,
                    href
                ):
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": href,
                    "fuente": fuente["nombre"]
                }

                # ========================
                # EVITAR REPETIDOS
                # ========================

                if noticia["link"] in data_enviadas["links"]:

                    print(f"REPETIDA LINK: {titulo}")

                    continue

                repetida = False

                for titulo_guardado in data_enviadas["titulos"]:

                    if titulo_parecido(
                        titulo,
                        titulo_guardado
                    ):

                        repetida = True
                        break

                if repetida:

                    print(f"REPETIDA TITULO: {titulo}")

                    continue

                noticias.append(noticia)

        except Exception as e:

            print(
                f"Error en {fuente['nombre']}: {e}"
            )

    return eliminar_duplicados(noticias)


# ========================
# TELEGRAM
# ========================

def enviar_mensaje(texto):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": texto,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False
        }
    )

    print(response.status_code)
    print(response.text)

    return response.status_code == 200


# ========================
# MAIN
# ========================

def main():

    print("Buscando noticias de Mexicali...")

    noticias = obtener_noticias()

    noticias_nuevas = []

    for noticia in noticias:

        if not ya_fue_enviada(noticia):
            noticias_nuevas.append(noticia)

    noticias_a_enviar = noticias_nuevas[:10]

    if not noticias_a_enviar:

        enviar_mensaje(
            "*No hay noticias nuevas de Mexicali.*"
        )

        return

    ahora = datetime.now().strftime(
        "%d/%m/%Y %H:%M"
    )

    encabezado = (
        f"*MEXICALI NOTICIAS*\n"
        f"*Fecha y hora:* {ahora}\n"
        f"*Cobertura:* Noticias Recientes"
    )

    enviar_mensaje(encabezado)

    time.sleep(2)

    for i, noticia in enumerate(
        noticias_a_enviar,
        1
    ):

        mensaje = (
            f"*{i}. {noticia['titulo']}*\n"
            f"Fuente: {noticia['fuente']}\n"
            f"Link: {noticia['link']}"
        )

        enviado = enviar_mensaje(
            mensaje
        )

        if enviado:
            guardar_enviada(
                noticia
            )

        time.sleep(1)


if __name__ == "__main__":
    main()