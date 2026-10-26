
"""
Bot de ofertas do Mercado Livre para Telegram.
Recebe links no privado e publica no grupo com:
foto, título, preço atual e botão de compra.
"""

import html
import json
import os
import re
import sys
import time

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()

GROUP_ID = os.environ.get(
    "TELEGRAM_CHAT_ID", "-1003727651878"
).strip()

ALLOWED = {
    int(x)
    for x in os.environ.get(
        "ALLOWED_USER_IDS", ""
    ).replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

API = f"https://api.telegram.org/bot{TOKEN}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


# ---------------- TELEGRAM ----------------

def tg(method, **data):
    try:
        response = requests.post(
            f"{API}/{method}",
            data=data,
            timeout=70,
        )
        resultado = response.json()

        if not resultado.get("ok"):
            print(
                f"Erro Telegram {method}: "
                f"{resultado.get('description')}"
            )

        return resultado

    except Exception as exc:
        print(f"Falha no Telegram: {exc}")
        return {"ok": False, "description": str(exc)}


def responder(chat_id, texto):
    tg(
        "sendMessage",
        chat_id=chat_id,
        text=texto,
        disable_web_page_preview="true",
    )


# ---------------- PREÇO E PRODUTO ----------------

def converter_preco(valor):
    if valor is None or valor == "":
        return None

    try:
        if isinstance(valor, (int, float)):
            numero = float(valor)
        else:
            texto = str(valor).strip()
            texto = texto.replace("R$", "").replace(" ", "")

            if "," in texto:
                texto = texto.replace(".", "")
                texto = texto.replace(",", ".")

            numero = float(texto)

        return numero if numero > 0 else None

    except (ValueError, TypeError):
        return None


def formatar_preco(valor):
    texto = f"{valor:,.2f}"
    texto = (
        texto.replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )

    if texto.endswith(",00"):
        texto = texto[:-3]

    return f"R$ {texto}"


def meta(texto_html, propriedade):
    padroes = [
        rf'<meta[^>]+(?:property|name|itemprop)='
        rf'["\']{re.escape(propriedade)}["\'][^>]*'
        rf'content=["\']([^"\']*)["\']',

        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*'
        rf'(?:property|name|itemprop)='
        rf'["\']{re.escape(propriedade)}["\']',
    ]

    for padrao in padroes:
        achado = re.search(padrao, texto_html, re.I)

        if achado:
            return html.unescape(
                achado.group(1)
            ).strip()

    return ""


def achar_produto(objeto):
    if isinstance(objeto, list):
        for item in objeto:
            encontrado = achar_produto(item)

            if encontrado:
                return encontrado

    elif isinstance(objeto, dict):
        tipo = objeto.get("@type")

        if tipo == "Product":
            return objeto

        if isinstance(tipo, list) and "Product" in tipo:
            return objeto

        for valor in objeto.values():
            encontrado = achar_produto(valor)

            if encontrado:
                return encontrado

    return None


def preco_das_ofertas(ofertas):
    if isinstance(ofertas, list):
        ofertas = ofertas[0] if ofertas else {}

    if not isinstance(ofertas, dict):
        return None

    for chave in ("price", "lowPrice"):
        preco = converter_preco(ofertas.get(chave))

        if preco is not None:
            return preco

    return None


def extrair_produto(texto_html):
    titulo = ""
    imagem = ""
    preco = None

    blocos = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>'
        r'(.*?)</script>',
        texto_html,
        re.S | re.I,
    )

    for bloco in blocos:
        try:
            dados = json.loads(bloco)
            produto = achar_produto(dados)
        except (ValueError, TypeError):
            continue

        if not produto:
            continue

        titulo = titulo or str(
            produto.get("name") or ""
        ).strip()

        img = produto.get("image")

        if isinstance(img, list):
            img = img[0] if img else ""

        if isinstance(img, dict):
            img = img.get("url", "")

        imagem = imagem or (img or "")

        if preco is None:
            preco = preco_das_ofertas(
                produto.get("offers")
            )

    titulo = titulo or meta(texto_html, "og:title")
    imagem = imagem or meta(texto_html, "og:image")

    if preco is None:
        for propriedade in (
            "product:price:amount",
            "og:price:amount",
            "price",
        ):
            preco = converter_preco(
                meta(texto_html, propriedade)
            )

            if preco is not None:
                break

    titulo = re.sub(
        r"\s*[|\-–]\s*Mercado Livre.*$",
        "",
        titulo,
        flags=re.I,
    ).strip()

    return {
        "titulo": titulo,
        "imagem": imagem,
        "preco": preco,
    }


# ---------------- API DO MERCADO LIVRE ----------------

def extrair_id_item(url):
    achado = re.search(
        r"\b(MLB-?\d{6,})\b",
        url,
        re.I,
    )

    if achado:
        return achado.group(1).upper().replace("-", "")

    return None


def consultar_api_ml(url):
    item_id = extrair_id_item(url)

    if not item_id:
        print(
            "Não foi possível identificar o ID do anúncio "
            "para consultar a API."
        )
        return {}

    try:
        resposta = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=HEADERS,
            timeout=20,
        )

        if resposta.status_code != 200:
            print(
                "API do Mercado Livre:",
                resposta.status_code,
                resposta.text[:300],
            )
            return {}

        dados = resposta.json()
        fotos = dados.get("pictures") or []
        imagem = ""

        if fotos:
            imagem = (
                fotos[0].get("secure_url")
                or fotos[0].get("url")
                or ""
            )

        preco = converter_preco(dados.get("price"))

        print(
            f"API do Mercado Livre: "
            f"preço encontrado = {preco}"
        )

        return {
            "titulo": dados.get("title") or "",
            "imagem": imagem,
            "preco": preco,
        }

    except Exception as exc:
        print("Erro ao consultar API:", repr(exc))
        return {}


def buscar_produto(url):
    try:
        resposta = requests.get(
            url,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Não consegui abrir o link: {exc}"
        )

    if resposta.status_code != 200:
        raise RuntimeError(
            f"O Mercado Livre respondeu HTTP "
            f"{resposta.status_code}."
        )

    url_final = resposta.url

    produto = extrair_produto(resposta.text)

    # Tenta completar os dados pela API.
    dados_api = consultar_api_ml(url_final)

    for campo in ("titulo", "imagem", "preco"):
        if produto.get(campo) in ("", None):
            valor = dados_api.get(campo)

            if valor not in ("", None):
                produto[campo] = valor

    # Não inventa preços.
    if produto.get("preco") is None:
        print(
            "AVISO: o preço atual não foi encontrado "
            "na página nem na API."
        )

    if not produto.get("titulo"):
        raise RuntimeError(
            "Não consegui encontrar o título do produto."
        )

    return produto


# ---------------- LEGENDA E PUBLICAÇÃO ----------------

def montar_legenda(produto):
    titulo = html.escape(
        produto.get("titulo") or "Oferta do Mercado Livre"
    )

    linhas = [f"🔥 {titulo}"]

    preco = produto.get("preco")

    if preco is not None:
        linhas.extend([
            "",
            f"💰 PREÇO ATUAL: {formatar_preco(preco)}",
        ])
    else:
        linhas.extend([
            "",
            "💰 Consulte o preço atualizado "
            "no botão Comprar.",
        ])

    return "\n".join(linhas)


def postar(produto, link):
    legenda = montar_legenda(produto)

    botao = json.dumps({
        "inline_keyboard": [[
            {
                "text": "🛒 Comprar",
                "url": link,
            }
        ]]
    })

    imagem = produto.get("imagem") or ""

    if imagem:
        resultado = tg(
            "sendPhoto",
            chat_id=GROUP_ID,
            photo=imagem,
            caption=legenda,
            parse_mode="HTML",
            reply_markup=botao,
        )

        if resultado.get("ok"):
            return True

        print(
            "Falha ao publicar foto:",
            resultado.get("description"),
        )

    resultado = tg(
        "sendMessage",
        chat_id=GROUP_ID,
        text=legenda,
        parse_mode="HTML",
        reply_markup=botao,
        disable_web_page_preview="true",
    )

    return resultado.get("ok", False)


# ---------------- RECEBER LINKS ----------------

def tratar_mensagem(msg):
    chat = msg.get("chat", {})

    if chat.get("type") != "private":
        return

    uid = msg.get("from", {}).get("id")
    texto = msg.get("text") or ""

    if uid not in ALLOWED:
        if not ALLOWED:
            responder(
                chat["id"],
                f"Seu ID do Telegram é: {uid}\n"
                "Adicione esse ID ao segredo "
                "ALLOWED_USER_IDS no GitHub.",
            )
        else:
            responder(
                chat["id"],
                "Você não tem permissão para usar este bot.",
            )

        return

    links = re.findall(r"https?://[^\s<>\"']+", texto)

    if not links:
        responder(
            chat["id"],
            "Envie um link de produto do Mercado Livre.",
        )
        return

    for link in links:
        link = link.rstrip(").,;")

        host = (
            requests.utils.urlparse(link).hostname or ""
        ).lower()

        if not (
            "mercadolivre.com" in host
            or "mercadolibre.com" in host
            or host.endswith("meli.la")
        ):
            responder(
                chat["id"],
                "Este bot aceita somente links do Mercado Livre.",
            )
            continue

        try:
            responder(
                chat["id"],
                "🔎 Buscando o produto e o preço atual...",
            )

            produto = buscar_produto(link)

            if postar(produto, link):
                responder(
                    chat["id"],
                    "✅ Produto publicado no grupo!",
                )
            else:
                responder(
                    chat["id"],
                    "❌ Não consegui publicar no grupo. "
                    "Confira o ID do grupo e as permissões do bot.",
                )

        except Exception as exc:
            print("Erro ao processar link:", repr(exc))

            responder(
                chat["id"],
                "❌ Não consegui processar esse produto.\n"
                f"Motivo: {str(exc)[:300]}",
            )

        time.sleep(2)


# ---------------- PROCESSAR ATUALIZAÇÕES ----------------

def processar(offset=None):
    total = 0

    while True:
        parametros = {
            "timeout": 0,
            "allowed_updates": json.dumps(["message"]),
            "limit": 100,
        }

        if offset is not None:
            parametros["offset"] = offset

        resultado = tg("getUpdates", **parametros)

        if not resultado.get("ok"):
            print(
                "Erro ao consultar mensagens:",
                resultado.get("description"),
            )
            break

        atualizacoes = resultado.get("result", [])

        if not atualizacoes:
            break

        for atualizacao in atualizacoes:
            offset = atualizacao["update_id"] + 1
            total += 1

            mensagem = atualizacao.get("message")

            if mensagem:
                try:
                    tratar_mensagem(mensagem)
                except Exception as exc:
                    print(
                        "Erro ao tratar mensagem:",
                        repr(exc),
                    )

    print(f"Atualizações processadas: {total}")
    return offset


def main():
    if not TOKEN:
        print("ERRO: configure TELEGRAM_TOKEN no GitHub.")
        sys.exit(1)

    resultado = tg("getMe")

    if not resultado.get("ok"):
        print(
            "ERRO: não consegui conectar ao Telegram. "
            "Confira TELEGRAM_TOKEN."
        )
        sys.exit(1)

    bot = resultado.get("result", {})
    print(f"Bot conectado: @{bot.get('username', '')}")

    if "--loop" in sys.argv:
        offset = None

        while True:
            offset = processar(offset)
            time.sleep(3)
    else:
        processar()


if __name__ == "__main__":
    main()
