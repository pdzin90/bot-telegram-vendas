"""
Bot que recebe LINKS do Mercado Livre no privado e posta no grupo com foto, titulo e preco.

Uso: voce manda o link (pode mandar varios, um por linha) numa conversa privada com o bot.
Texto escrito junto com UM link vira uma observacao extra na postagem.

Modos:
  python ml_bot.py          -> processa o que chegou e sai (GitHub Actions)
  python ml_bot.py --loop   -> fica ouvindo o tempo todo, resposta quase instantanea (PC ou hospedagem 24h)

Variaveis de ambiente:
  TELEGRAM_TOKEN     token do bot (obrigatorio)
  TELEGRAM_CHAT_ID   id do grupo (padrao: o seu grupo)
  ALLOWED_USER_IDS   ids de quem pode mandar links, separados por virgula
                     (se vazio, o bot so responde mostrando o id de quem escreveu)
"""

import html as htmllib
import json
import os
import re
import sys
import time

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROUP_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003727651878")
ALLOWED = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()
}
API = f"https://api.telegram.org/bot{TOKEN}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+")


# ---------------------------------------------------------------- Telegram
def tg(method, **data):
    try:
        r = requests.post(f"{API}/{method}", data=data, timeout=70)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def responder(chat_id, texto):
    tg("sendMessage", chat_id=chat_id, text=texto, disable_web_page_preview="true")


# ---------------------------------------------------------------- leitura da pagina
def meta(texto, prop):
    for padrao in (
        rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(prop)}["\']',
    ):
        m = re.search(padrao, texto, re.I)
        if m:
            return htmllib.unescape(m.group(1)).strip()
    return ""


def _achar_produto(obj):
    if isinstance(obj, list):
        for x in obj:
            p = _achar_produto(x)
            if p:
                return p
    elif isinstance(obj, dict):
        tipo = obj.get("@type")
        if tipo == "Product" or (isinstance(tipo, list) and "Product" in tipo):
            return obj
        for v in obj.values():
            p = _achar_produto(v)
            if p:
                return p
    return None


def _preco_de_offers(offers):
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None
    for chave in ("price", "lowPrice"):
        if offers.get(chave) not in (None, ""):
            try:
                return float(str(offers[chave]).replace(",", "."))
            except ValueError:
                pass
    return None


def formatar_preco(v):
    s = f"{v:,.2f}"                      # 1,234.50
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + (s[:-3] if s.endswith(",00") else s)


def extrair_produto(texto_html):
    titulo = imagem = ""
    preco = None

    for bloco in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', texto_html, re.S | re.I):
        try:
            prod = _achar_produto(json.loads(bloco))
        except Exception:
            continue
        if not prod:
            continue
        titulo = titulo or str(prod.get("name") or "").strip()
        img = prod.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url", "")
        imagem = imagem or (img or "")
        preco = preco if preco is not None else _preco_de_offers(prod.get("offers"))

    titulo = titulo or meta(texto_html, "og:title")
    imagem = imagem or meta(texto_html, "og:image")
    if preco is None:
        for prop in ("product:price:amount", "price", "og:price:amount"):
            valor = meta(texto_html, prop)
            if valor:
                try:
                    preco = float(valor.replace(",", "."))
                    break
                except ValueError:
                    pass

    titulo = re.sub(r"\s*[|\-–]\s*Mercado\s*Livre.*$", "", titulo, flags=re.I).strip()
    return {"titulo": titulo, "imagem": imagem, "preco": preco}


def buscar_produto(url):
    r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"a pagina respondeu {r.status_code}")
    prod = extrair_produto(r.text)
    if not prod["titulo"]:
        raise RuntimeError("nao consegui ler o titulo do produto (pagina diferente do esperado ou bloqueada)")
    prod["url_final"] = r.url
    return prod


# ---------------------------------------------------------------- postagem
def montar_legenda(prod, extra=""):
    linhas = [f"🔥 {prod['titulo']}"]
    if prod["preco"] is not None:
        linhas += ["", f"💰 {formatar_preco(prod['preco'])}"]
    if extra:
        linhas += ["", extra]
    return "\n".join(linhas)


def postar(prod, link, extra=""):
    legenda = montar_legenda(prod, extra)
    botao = json.dumps({"inline_keyboard": [[{"text": "🛒 Comprar", "url": link}]]})

    imagem = prod["imagem"]
    if imagem:
        candidatas = []
        if imagem.lower().endswith(".webp"):
            candidatas.append(imagem[:-5] + ".jpg")
        candidatas.append(imagem)
        for foto in candidatas:
            res = tg("sendPhoto", chat_id=GROUP_ID, photo=foto, caption=legenda[:1024], reply_markup=botao)
            if res.get("ok"):
                return True, ""
    res = tg("sendMessage", chat_id=GROUP_ID, text=legenda[:4096], reply_markup=botao,
             disable_web_page_preview="true")
    return res.get("ok", False), res.get("description", "")


def tratar_mensagem(msg):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return                                   # ignora o que acontece dentro de grupos
    uid = msg.get("from", {}).get("id")
    texto = msg.get("text") or msg.get("caption") or ""

    if uid not in ALLOWED:
        if ALLOWED:
            responder(chat["id"], "Sem permissão para usar este bot.")
        else:
            responder(chat["id"], f"Seu id do Telegram é: {uid}\n"
                                  "Cadastre esse número no segredo ALLOWED_USER_IDS para liberar o bot.")
        return

    links = URL_RE.findall(texto)
    if not links:
        responder(chat["id"], "Me envie o link do produto do Mercado Livre (pode mandar vários, um por linha).")
        return

    extra = URL_RE.sub("", texto).strip() if len(links) == 1 else ""
    for link in links:
        link = link.rstrip(").,;")
        try:
            prod = buscar_produto(link)
            ok, erro = postar(prod, link, extra)
            if ok:
                aviso = "" if prod["preco"] is not None else "\n(atenção: não achei o preço, postei sem ele)"
                responder(chat["id"], f"✅ Postado: {prod['titulo'][:60]}{aviso}")
            else:
                responder(chat["id"], f"❌ Não consegui postar no grupo: {erro}")
        except Exception as e:
            responder(chat["id"], f"❌ Não consegui ler esse link ({e}):\n{link}")
        time.sleep(2)


def processar(offset=None, espera=0):
    params = {"timeout": espera, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    res = tg("getUpdates", **params)
    if not res.get("ok"):
        print("Erro no getUpdates:", res.get("description"))
        return offset
    ultimo = None
    for up in res.get("result", []):
        ultimo = up["update_id"]
        if "message" in up:
            try:
                tratar_mensagem(up["message"])
            except Exception as e:
                print("Erro tratando mensagem:", e)
    if ultimo is not None:
        offset = ultimo + 1
        tg("getUpdates", offset=offset, timeout=0)   # confirma: o Telegram nao reenvia estas mensagens
    return offset


def main():
    if not TOKEN:
        print("Faltou o token. Defina TELEGRAM_TOKEN.")
        sys.exit(1)
    if "--loop" in sys.argv:
        print("Ouvindo mensagens. Ctrl+C para parar.")
        offset = None
        while True:
            offset = processar(offset, espera=50)
    else:
        processar()


if __name__ == "__main__":
    main()
