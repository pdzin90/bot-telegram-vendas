"""
Bot que recebe links do Mercado Livre ou Shopee no privado e posta no grupo com foto,
título, preço quando disponível e botão de compra.

No GitHub Actions, execute: python ml_bot.py
O programa consulta as mensagens pendentes, responde ao usuário e termina.

Variáveis de ambiente:
  TELEGRAM_TOKEN     token do bot (obrigatório)
  TELEGRAM_CHAT_ID   ID do grupo (padrão: -1003727651878)
  ALLOWED_USER_IDS   IDs autorizados, separados por vírgula.
                     Se vazio, o bot responde mostrando o ID de quem escreveu.
"""

import html as htmllib
import json
import os
import re
import sys
import time

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GROUP_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003727651878").strip()
ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
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


# ------------------------------- Telegram
def tg(method, **data):
    """Chama a API do Telegram e devolve a resposta, incluindo erros."""
    try:
        response = requests.post(f"{API}/{method}", data=data, timeout=70)
        try:
            result = response.json()
        except ValueError:
            return {
                "ok": False,
                "description": f"Resposta não JSON do Telegram (HTTP {response.status_code})",
            }
        if not result.get("ok"):
            print(f"Telegram {method}: {result.get('description', 'erro sem descrição')}")
        return result
    except Exception as exc:
        print(f"Falha de conexão em {method}: {exc}")
        return {"ok": False, "description": str(exc)}


def responder(chat_id, texto):
    resultado = tg(
        "sendMessage",
        chat_id=chat_id,
        text=texto,
        disable_web_page_preview="true",
    )
    if not resultado.get("ok"):
        print("Não consegui enviar mensagem privada:", resultado.get("description"))
    else:
        print(f"Mensagem enviada com sucesso para o chat {chat_id}.")


# ------------------------------- Leitura da página do produto
def meta(texto, prop):
    for padrao in (
        rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(prop)}["\']',
    ):
        match = re.search(padrao, texto, re.I)
        if match:
            return htmllib.unescape(match.group(1)).strip()
    return ""


def _achar_produto(obj):
    if isinstance(obj, list):
        for item in obj:
            produto = _achar_produto(item)
            if produto:
                return produto
    elif isinstance(obj, dict):
        tipo = obj.get("@type")
        if tipo == "Product" or (isinstance(tipo, list) and "Product" in tipo):
            return obj
        for valor in obj.values():
            produto = _achar_produto(valor)
            if produto:
                return produto
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


def formatar_preco(valor):
    texto = f"{valor:,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + (texto[:-3] if texto.endswith(",00") else texto)


def extrair_produto(texto_html):
    titulo = imagem = ""
    preco = None

    blocos = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
        texto_html,
        re.S | re.I,
    )
    for bloco in blocos:
        try:
            produto = _achar_produto(json.loads(bloco))
        except Exception:
            continue
        if not produto:
            continue
        titulo = titulo or str(produto.get("name") or "").strip()
        img = produto.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url", "")
        imagem = imagem or (img or "")
        if preco is None:
            preco = _preco_de_offers(produto.get("offers"))

    titulo = titulo or meta(texto_html, "og:title")
    imagem = imagem or meta(texto_html, "og:image")
    if preco is None:
        for propriedade in ("product:price:amount", "price", "og:price:amount"):
            valor = meta(texto_html, propriedade)
            if valor:
                try:
                    preco = float(valor.replace(",", "."))
                    break
                except ValueError:
                    pass

    titulo = re.sub(r"\s*[|\-–]\s*Mercado Livre.*$", "", titulo, flags=re.I).strip()
    return {"titulo": titulo, "imagem": imagem, "preco": preco}



def _id_item_ml(url):
    """Extrai um ID MLB de uma URL final do Mercado Livre, quando existir."""
    match = re.search(r"\b(MLB-?\d{6,})\b", url, re.I)
    return match.group(1).upper().replace("-", "") if match else None


def _consultar_api_ml(url):
    """
    Tenta obter título, imagem e preço pela API pública do Mercado Livre.
    Pode não funcionar para todos os anúncios ou regiões; por isso é fallback.
    """
    item_id = _id_item_ml(url)
    if not item_id:
        return {}
    try:
        resposta = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=HEADERS,
            timeout=20,
        )
        if resposta.status_code != 200:
            print(f"API Mercado Livre: HTTP {resposta.status_code} para {item_id}")
            return {}
        dados = resposta.json()
        fotos = dados.get("pictures") or []
        imagem = ""
        if fotos:
            imagem = fotos[0].get("secure_url") or fotos[0].get("url") or ""
        valor = dados.get("price")
        try:
            valor = float(valor) if valor is not None else None
        except (TypeError, ValueError):
            valor = None
        return {
            "titulo": str(dados.get("title") or "").strip(),
            "imagem": imagem,
            "preco": valor,
        }
    except Exception as exc:
        print("Não consegui consultar a API do Mercado Livre:", repr(exc))
        return {}


def _preco_embutido_html(texto_html):
    """Procura preços em metadados comuns e em campos JSON embutidos."""
    for propriedade in (
        "product:price:amount", "og:price:amount", "price",
        "product:sale_price:amount",
    ):
        valor = meta(texto_html, propriedade)
        if valor:
            try:
                return float(valor.replace(".", "").replace(",", ".")) if "," in valor else float(valor)
            except ValueError:
                pass

    # Alguns templates embutem campos de preço fora do JSON-LD.
    padroes = (
        r'"price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?',
        r'"sale_price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?',
        r'"amount"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?',
    )
    for padrao in padroes:
        for achado in re.findall(padrao, texto_html, re.I):
            try:
                valor = achado.replace(",", ".")
                numero = float(valor)
                # Ignora números improváveis que possam ser IDs ou outras métricas.
                if 0 < numero < 10000000:
                    return numero
            except ValueError:
                continue
    return None


def identificar_loja(url):
    host = re.sub(r"^www\.", "", (requests.utils.urlparse(url).hostname or "").lower())
    if any(dominio in host for dominio in ("shopee.com", "shope.ee")):
        return "Shopee"
    if any(dominio in host for dominio in ("mercadolivre.com", "mercadolibre.com", "meli.la")):
        return "Mercado Livre"
    return "loja"


def _preco_shopee_html(texto_html):
    """Extrai apenas campos de preço conhecidos da página da Shopee.

    A Shopee pode guardar preços em centavos nos campos price_min/price_max.
    Evita usar números genéricos do HTML, que poderiam ser IDs ou contagens.
    """
    for propriedade in ("product:price:amount", "og:price:amount", "price"):
        valor = meta(texto_html, propriedade)
        if valor:
            try:
                numero = float(valor.replace(",", "."))
                if numero > 0:
                    return numero
            except ValueError:
                pass

    # Campos comuns nos dados estruturados da Shopee. Eles normalmente estão em centavos.
    for campo in ("price_min", "price_max"):
        padrao = rf'''["\']{campo}["\']\s*:\s*["\']?(\d+(?:\.\d+)?)["\']?'''
        for achado in re.findall(padrao, texto_html, re.I):
            try:
                numero = float(achado)
            except ValueError:
                continue
            if numero <= 0:
                continue
            # Valores inteiros grandes em campos da Shopee costumam estar em centavos.
            if campo in ("price_min", "price_max") and numero >= 1000:
                numero /= 100
            if numero < 100000:
                return numero
    return None


def buscar_produto(url):
    loja = identificar_loja(url)
    resposta = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    if resposta.status_code != 200:
        raise RuntimeError(f"a página respondeu HTTP {resposta.status_code}; a loja pode bloquear acessos automáticos")

    produto = extrair_produto(resposta.text)
    produto["url_final"] = resposta.url
    produto["loja"] = identificar_loja(resposta.url) if identificar_loja(resposta.url) != "loja" else loja

    # Tenta fontes de preço adequadas à loja, sem inventar um preço.
    if produto.get("preco") is None and produto["loja"] == "Shopee":
        produto["preco"] = _preco_shopee_html(resposta.text)
    elif produto.get("preco") is None:
        produto["preco"] = _preco_embutido_html(resposta.text)

    # O link curto meli.la costuma redirecionar para a página do item;
    # se houver ID MLB na URL final, tenta a API pública como alternativa.
    api_produto = _consultar_api_ml(resposta.url) if produto["loja"] == "Mercado Livre" else {}
    for campo in ("titulo", "imagem", "preco"):
        if produto.get(campo) in ("", None) and api_produto.get(campo) not in ("", None):
            produto[campo] = api_produto[campo]

    if not produto.get("titulo"):
        raise RuntimeError(
            f"não consegui ler o título do produto da {produto['loja']} (a página pode exigir login, estar bloqueada ou carregar os dados por JavaScript)"
        )
    if produto.get("preco") is None:
        print("Aviso: o preço não foi encontrado no HTML nem na API pública.")
    return produto


# ------------------------------- Postagem no grupo
def montar_legenda(produto, extra=""):
    linhas = [f"🔥 {produto['titulo']}"]
    if produto["preco"] is not None:
        linhas += ["", f"💰 {formatar_preco(produto['preco'])}"]
    else:
        linhas += ["", "💰 Consulte o preço atualizado no botão Comprar"]
    if extra:
        linhas += ["", extra]
    return "\n".join(linhas)


def postar(produto, link, extra=""):
    legenda = montar_legenda(produto, extra)
    botao = json.dumps(
        {"inline_keyboard": [[{"text": "🛒 Comprar", "url": link}]]}
    )

    imagem = produto["imagem"]
    if imagem:
        candidatas = []
        if imagem.lower().endswith(".webp"):
            candidatas.append(imagem[:-5] + ".jpg")
        candidatas.append(imagem)
        for foto in candidatas:
            resultado = tg(
                "sendPhoto",
                chat_id=GROUP_ID,
                photo=foto,
                caption=legenda[:1024],
                reply_markup=botao,
            )
            if resultado.get("ok"):
                return True, ""
            print("Falha ao postar foto; tentando alternativa.")

    resultado = tg(
        "sendMessage",
        chat_id=GROUP_ID,
        text=legenda[:4096],
        reply_markup=botao,
        disable_web_page_preview="true",
    )
    return resultado.get("ok", False), resultado.get("description", "")


def tratar_mensagem(msg):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        print("Mensagem ignorada: não veio de conversa privada.")
        return

    uid = msg.get("from", {}).get("id")
    texto = msg.get("text") or msg.get("caption") or ""
    print(f"Mensagem privada recebida. ID do usuário: {uid}; texto recebido: {bool(texto)}")

    if uid not in ALLOWED:
        if ALLOWED:
            responder(chat["id"], "Sem permissão para usar este bot.")
        else:
            responder(
                chat["id"],
                f"Seu ID do Telegram é: {uid}\n"
                "Cadastre esse número no segredo ALLOWED_USER_IDS para liberar o bot.",
            )
        return

    links = URL_RE.findall(texto)
    if not links:
        responder(
            chat["id"],
            "Me envie um link de produto do Mercado Livre ou da Shopee (pode mandar vários, um por linha).",
        )
        return

    extra = URL_RE.sub("", texto).strip() if len(links) == 1 else ""
    for link in links:
        link = link.rstrip(").,;")
        try:
            produto = buscar_produto(link)
            ok, erro = postar(produto, link, extra)
            if ok:
                aviso = (
                    ""
                    if produto["preco"] is not None
                    else "\n(Atenção: não encontrei o preço, então publiquei sem ele.)"
                )
                responder(chat["id"], f"✅ Postado ({produto.get('loja', 'loja')}): {produto['titulo'][:60]}{aviso}")
            else:
                responder(chat["id"], f"❌ Não consegui postar no grupo: {erro}")
        except Exception as exc:
            responder(chat["id"], f"❌ Não consegui ler esse link ({exc}):\n{link}")
        time.sleep(2)


# ------------------------------- Processamento de mensagens
def processar(offset=None, espera=0):
    """
    Processa todos os lotes de atualizações pendentes.
    O offset é avançado lote a lote para não parar nos primeiros 100
    eventos de grupo antes de chegar a mensagens privadas mais recentes.
    """
    total = 0
    privados = 0
    grupos = 0

    while True:
        parametros = {
            "timeout": espera if total == 0 else 0,
            "allowed_updates": json.dumps(["message"]),
            "limit": 100,
        }
        if offset is not None:
            parametros["offset"] = offset

        print("Consultando mensagens pendentes no Telegram...")
        resultado = tg("getUpdates", **parametros)
        if not resultado.get("ok"):
            print("ERRO em getUpdates:", resultado.get("description"))
            return offset

        atualizacoes = resultado.get("result", [])
        print(f"Lote recebido: {len(atualizacoes)} atualização(ões).")
        if not atualizacoes:
            break

        for atualizacao in atualizacoes:
            offset = atualizacao["update_id"] + 1
            total += 1
            mensagem = atualizacao.get("message")
            if not mensagem:
                continue

            tipo_chat = mensagem.get("chat", {}).get("type")
            if tipo_chat == "private":
                privados += 1
                try:
                    tratar_mensagem(mensagem)
                except Exception as exc:
                    print("ERRO ao tratar mensagem privada:", repr(exc))
            else:
                grupos += 1
                # As mensagens de grupo são ignoradas de propósito; o bot
                # recebe links apenas por conversa privada.

        # A próxima chamada com offset confirma apenas o lote já processado
        # e busca o próximo lote, caso ainda existam atualizações pendentes.
        # Faz outra consulta mesmo quando o lote veio menor que 100:
        # essa chamada confirma o lote anterior e verifica se há mais eventos.
        # O loop termina quando a consulta seguinte retornar lista vazia.

    print(
        f"Resumo: {total} atualização(ões) processada(s), "
        f"{privados} mensagem(ns) privada(s), {grupos} mensagem(ns) de grupo ignorada(s)."
    )
    return offset


def main():
    if not TOKEN:
        print("ERRO: faltou o token. Configure o segredo TELEGRAM_TOKEN.")
        sys.exit(1)

    print("Testando o token do Telegram...")
    me = tg("getMe")
    if not me.get("ok"):
        print("ERRO: o Telegram não aceitou o token. Confira o segredo TELEGRAM_TOKEN.")
        sys.exit(1)

    bot = me.get("result", {})
    print(f"Token válido. Bot conectado: @{bot.get('username', 'sem_username')}")
    print("Iniciando leitura de mensagens...")

    if "--loop" in sys.argv:
        print("Modo contínuo ativado. Ctrl+C para parar.")
        offset = None
        while True:
            offset = processar(offset, espera=50)
    else:
        processar()


if __name__ == "__main__":
    main()
