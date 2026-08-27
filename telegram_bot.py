import os, re, json, time, logging
from io import BytesIO
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('mexicali_news_bot')

TOKEN = os.getenv('TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
ARCHIVO_ENVIADAS = 'noticias_enviadas_mexicali.json'
TZ = ZoneInfo('America/Tijuana')
MAX_NOTICIAS = 10
MAX_HISTORIAL = 1500
UMBRAL_SIMILITUD = 0.80
MAX_IMAGE_BYTES = 18 * 1024 * 1024

# Distribución objetivo: 4 + 3 + 3 = 10
CUOTAS = {
    'La Voz de la Frontera': 4,
    'El Imparcial': 3,
    'La Crónica': 3,
}

FUENTES = [
    {'medio':'La Voz de la Frontera','nombre':'La Voz - Local','url':'https://www.lavozdelafrontera.com.mx/local/'},
    {'medio':'La Voz de la Frontera','nombre':'La Voz - Policiaca','url':'https://www.lavozdelafrontera.com.mx/policiaca/'},
    {'medio':'La Voz de la Frontera','nombre':'La Voz - Deportes','url':'https://www.lavozdelafrontera.com.mx/deportes/'},
    {'medio':'El Imparcial','nombre':'El Imparcial - Mexicali','url':'https://www.elimparcial.com/mexicali/'},
    {'medio':'El Imparcial','nombre':'El Imparcial - Policiaca','url':'https://www.elimparcial.com/mxl/policiaca/'},
    {'medio':'La Crónica','nombre':'La Crónica - Mexicali','url':'https://www.lacronica.com/mexicali/'},
    {'medio':'La Crónica','nombre':'La Crónica - Policiaca','url':'https://www.lacronica.com/mxl/policiaca/'},
]

HEADERS = {
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36',
    'Accept-Language':'es-MX,es;q=0.9,en;q=0.8',
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def limpiar_texto(t):
    t = str(t or '').lower()
    for a,b in {'á':'a','é':'e','í':'i','ó':'o','ú':'u','ü':'u','ñ':'n'}.items():
        t=t.replace(a,b)
    t=re.sub(r'[^a-z0-9\s]',' ',t)
    return re.sub(r'\s+',' ',t).strip()


def escapar_html(t):
    return str(t or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')


def titulo_parecido(a,b):
    a,b=limpiar_texto(a),limpiar_texto(b)
    return bool(a and b and SequenceMatcher(None,a,b).ratio() >= UMBRAL_SIMILITUD)


def historial_vacio():
    return {'ultima_ejecucion':None,'ultimo_total_encontrado':0,'ultimo_total_enviado':0,'links':[],'titulos':[]}


def cargar_historial():
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return historial_vacio()
    try:
        with open(ARCHIVO_ENVIADAS,'r',encoding='utf-8') as f:
            d=json.load(f)
        base=historial_vacio(); base.update(d if isinstance(d,dict) else {})
        return base
    except Exception as e:
        log.error('Error leyendo historial: %s',e)
        return historial_vacio()


def guardar_historial(d):
    d['links']=d.get('links',[])[-MAX_HISTORIAL:]
    d['titulos']=d.get('titulos',[])[-MAX_HISTORIAL:]
    tmp=ARCHIVO_ENVIADAS+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:
        json.dump(d,f,ensure_ascii=False,indent=2); f.write('\n')
    os.replace(tmp,ARCHIVO_ENVIADAS)


class Historial:
    def __init__(self):
        d=cargar_historial()
        self.links=set(d.get('links',[])); self.titulos=list(d.get('titulos',[]))
    def ya(self,n):
        if n['link'] in self.links: return True
        return any(titulo_parecido(n['titulo'],t) for t in self.titulos)
    def registrar(self,n):
        self.links.add(n['link'])
        if n['titulo'] not in self.titulos: self.titulos.append(n['titulo'])
    def guardar(self,encontrados=0,enviados=0):
        guardar_historial({'ultima_ejecucion':datetime.now(TZ).isoformat(),'ultimo_total_encontrado':encontrados,'ultimo_total_enviado':enviados,'links':list(self.links),'titulos':self.titulos})


def es_mexicali(titulo,link):
    x=limpiar_texto(titulo+' '+link)
    claves=['mexicali','valle de mexicali','cachanilla','palaco','calexico','nuevo mexicali','pueblo nuevo','garita','aduana']
    excluir=['tijuana','ensenada','rosarito','tecate','san felipe','san quintin','san luis rio colorado','slrc','hermosillo']
    if any(c in x for c in excluir) and 'mexicali' not in x: return False
    return any(c in x for c in claves)


def fecha_url(url):
    m=re.search(r'/(\d{4})/(\d{2})/(\d{2})/',url)
    if not m:return None
    try:return datetime(int(m.group(1)),int(m.group(2)),int(m.group(3)),tzinfo=TZ)
    except:return None


def convertir_fecha(v):
    if not v:return None
    try:
        v=str(v).strip().replace('Z','+00:00')
        d=datetime.fromisoformat(v)
        if d.tzinfo is None:d=d.replace(tzinfo=TZ)
        return d.astimezone(TZ)
    except:return None


def fecha_soup(soup):
    for attrs in [
        {'property':'article:published_time'},{'property':'article:modified_time'},
        {'name':'date'},{'name':'pubdate'},{'itemprop':'datePublished'}]:
        m=soup.find('meta',attrs=attrs)
        if m and m.get('content'):
            d=convertir_fecha(m['content'])
            if d:return d
    for s in soup.find_all('script',type='application/ld+json'):
        m=re.search(r'"datePublished"\s*:\s*"([^"]+)"',s.get_text(' ',strip=True))
        if m:
            d=convertir_fecha(m.group(1))
            if d:return d
    return None


def imagen_soup(soup,url):
    for attrs in [
        {'property':'og:image'},{'property':'og:image:secure_url'},
        {'name':'twitter:image'},{'name':'twitter:image:src'}]:
        m=soup.find('meta',attrs=attrs)
        if m and m.get('content'):
            return urljoin(url,m['content'].strip())
    for s in soup.find_all('script',type='application/ld+json'):
        txt=s.get_text(' ',strip=True)
        for pat in [r'"image"\s*:\s*"([^"]+)"',r'"thumbnailUrl"\s*:\s*"([^"]+)"']:
            m=re.search(pat,txt)
            if m:return urljoin(url,m.group(1).replace('\\/','/'))
    art=soup.find('article') or soup.find('main') or soup
    for img in art.find_all('img'):
        for a in ['data-src','data-lazy-src','data-original','src']:
            if img.get(a): return urljoin(url,img.get(a).strip())
    return None


def parece_articulo(link,medio):
    p=urlparse(link).path.lower().rstrip('/')
    if medio=='La Voz de la Frontera':
        return bool(re.search(r'/lavozdelafrontera/(local|policiaca|deportes)/.+-\d{6,}$',p))
    if medio=='El Imparcial':
        return bool(re.search(r'/mxl/(mexicali|policiaca)/\d{4}/\d{2}/\d{2}/',p))
    if medio=='La Crónica':
        return ('/mxl/mexicali/' in p or '/mxl/policiaca/' in p or '/mexicali/' in p or '/policiaca/' in p)
    return False


def dedupe(lista):
    out=[]
    for n in lista:
        if any(n['link']==x['link'] or titulo_parecido(n['titulo'],x['titulo']) for x in out):
            continue
        out.append(n)
    return out


def recolectar(hist):
    grupos={m:[] for m in CUOTAS}
    for f in FUENTES:
        try:
            log.info('Leyendo %s',f['nombre'])
            r=SESSION.get(f['url'],timeout=20,allow_redirects=True)
            log.info('%s -> HTTP %s',f['nombre'],r.status_code)
            if r.status_code!=200: continue
            soup=BeautifulSoup(r.text,'html.parser')
            for a in soup.find_all('a',href=True):
                titulo=a.get_text(' ',strip=True); href=a.get('href','').strip()
                if not titulo or len(titulo)<20: continue
                link=urljoin(r.url,href)
                if not parece_articulo(link,f['medio']): continue
                if not es_mexicali(titulo,link): continue
                n={'titulo':titulo,'link':link,'medio':f['medio'],'fecha':fecha_url(link),'imagen':None}
                if not hist.ya(n): grupos[f['medio']].append(n)
        except Exception as e:
            log.warning('Error en %s: %s',f['nombre'],e)
    for m in grupos:
        grupos[m]=dedupe(grupos[m])
        log.info('%s candidatas: %d',m,len(grupos[m]))
    return grupos


def enriquecer(n):
    try:
        r=SESSION.get(n['link'],timeout=20,allow_redirects=True); r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        n['link']=r.url
        n['fecha']=fecha_soup(soup) or fecha_url(r.url) or n.get('fecha')
        n['imagen']=imagen_soup(soup,r.url)
    except Exception as e:
        log.warning('No se pudo enriquecer %s: %s',n['titulo'],e)


def seleccionar_balanceado(grupos):
    minimo=datetime.min.replace(tzinfo=TZ)
    # Enriquecer suficientes candidatos de cada medio antes de seleccionar
    for medio,lista in grupos.items():
        for n in lista[:max(12,CUOTAS[medio]*4)]:
            enriquecer(n); time.sleep(.1)
        lista.sort(key=lambda n:n.get('fecha') or minimo, reverse=True)

    sel=[]
    # Cuotas estrictas 4/3/3
    for medio in ['La Voz de la Frontera','El Imparcial','La Crónica']:
        sel.extend(grupos[medio][:CUOTAS[medio]])

    # Si una fuente no alcanzó su cuota, completar de forma rotativa,
    # priorizando primero las fuentes NO Imparcial.
    while len(sel)<MAX_NOTICIAS:
        agregado=False
        for medio in ['La Voz de la Frontera','La Crónica','El Imparcial']:
            usados=sum(1 for n in sel if n['medio']==medio)
            if usados < len(grupos[medio]):
                candidato=grupos[medio][usados]
                if candidato not in sel:
                    sel.append(candidato); agregado=True
                    if len(sel)>=MAX_NOTICIAS: break
        if not agregado: break

    sel=dedupe(sel)[:MAX_NOTICIAS]
    sel.sort(key=lambda n:n.get('fecha') or minimo, reverse=True)
    for medio in CUOTAS:
        log.info('FINAL %s: %d',medio,sum(1 for n in sel if n['medio']==medio))
    return sel


def descargar_imagen(url,referer):
    if not url:return None
    try:
        r=SESSION.get(url,headers={'User-Agent':HEADERS['User-Agent'],'Referer':referer},timeout=25,stream=True,allow_redirects=True); r.raise_for_status()
        data=bytearray()
        for ch in r.iter_content(65536):
            if ch:data.extend(ch)
            if len(data)>MAX_IMAGE_BYTES:return None
        with Image.open(BytesIO(bytes(data))) as im:
            im.load(); im=ImageOps.exif_transpose(im)
            if im.mode!='RGB':im=im.convert('RGB')
            out=BytesIO(); im.save(out,'JPEG',quality=90,optimize=True); out.seek(0); return out
    except Exception as e:
        log.warning('Imagen falló: %s',e); return None


def validar(r):
    try:p=r.json()
    except:return False
    if r.status_code!=200 or not p.get('ok'):
        log.error('Telegram: %s',p); return False
    return True


def enviar_mensaje(texto,preview=False):
    return validar(requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',data={'chat_id':CHAT_ID,'text':texto,'parse_mode':'HTML','disable_web_page_preview':not preview},timeout=30))


def enviar_noticia(n):
    titulo=escapar_html(n['titulo']); fuente=escapar_html(n['medio']); link=escapar_html(n['link'])
    caption=f'<b>{titulo}</b>\nFuente: {fuente}\n<a href="{link}">Abrir noticia</a>'
    if n.get('imagen'):
        foto=descargar_imagen(n['imagen'],n['link'])
        if foto:
            r=requests.post(f'https://api.telegram.org/bot{TOKEN}/sendPhoto',data={'chat_id':CHAT_ID,'caption':caption,'parse_mode':'HTML'},files={'photo':('noticia.jpg',foto,'image/jpeg')},timeout=60)
            if validar(r):return True
    return enviar_mensaje(f'<b>{titulo}</b>\nFuente: {fuente}\nLink: {link}',True)


def main():
    if not TOKEN or not CHAT_ID:
        log.error('Falta TOKEN o CHAT_ID'); return
    hist=Historial(); hist.guardar(0,0)
    grupos=recolectar(hist)
    noticias=seleccionar_balanceado(grupos)
    hist.guardar(len(noticias),0)
    if not noticias:
        log.info('No hay noticias nuevas'); return
    fecha=datetime.now(TZ).strftime('%d/%m/%Y')
    enviar_mensaje(f'<b>MEXICALI NOTICIAS</b>\n<b>Fecha:</b> {fecha}',False)
    time.sleep(2)
    enviadas=0
    for n in noticias:
        if enviar_noticia(n):
            hist.registrar(n); enviadas+=1; hist.guardar(len(noticias),enviadas)
        time.sleep(1)
    hist.guardar(len(noticias),enviadas)
    log.info('Enviadas %d de %d',enviadas,len(noticias))

if __name__=='__main__':
    main()
