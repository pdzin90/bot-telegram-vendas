
import os
import re
import json
import html
import time
import sys
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GROUP_ID = os.environ.get(
    "TELEGRAM_CHAT_ID", "-1003727651878"
).strip()

ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

API = f"https://api.telegram.org/bot{TOKEN}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


# ---------- TELEGRAM ----------

def telegram(metodo, **dados):
    try:
        r = requests.post(
            f"{API}/{metodo}",
            data=dados,
            timeout=60,
        )
        resultado = r.json()

        if not resultado.get("ok"):
            print(
                f"Erro Telegram ({metodo}): "
                f"{resultado.get('description')}"
            )

        return resultado

    except Exception as e:
        print("Erro de conexão com Telegram:", repr(e))
        return {"ok": False}


def responder(chat_id, texto):
    return telegram(
        "sendMessage",
        chat_id=chat_id,
        text=texto,
    )


# ---------- PREÇOS ----------

def converter_preco(valor):
    if valor is None or isinstance(valor, bool):
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

        if numero <= 0 or numero > 100000000:
            return None

        return numero

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


def extrair_preco_json(conteudo):
    """
    Procura preços em estruturas JSON e dados incorporados
    na página. Aceita apenas valores associados a campos
    com nomes relacionados a preço.
    """

    padroes = [
        r'"price"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
        r'"priceAmount"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
        r'"salePrice"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
        r'"current_price"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
        r'"price_with_currency"\s*:\s*"R\$\s*([\d.,]+)"',
    ]

    for padrao in padroes:
        for encontrado in re.finditer(
            padrao, conteudo, re.I
        ):
            valor = converter_preco(encontrado.group(1))

            if valor is not None:
                return valor

    return None


def obter_meta(conteudo, propriedade):
    padroes = [
        rf'<meta[^>]+(?:property|name|itemprop)='
        rf'["\']{re.escape(propriedade)}["\'][^>]*'
        rf'content=["\']([^"\']*)["\']',

        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*'
        rf'(?:property|name|itemprop)='
        rf'["\']{re.escape(propriedade)}["\']',
    ]

    for padrao in padroes:
        achado = re.search(padrao, conteudo, re.I)

        if achado:
            return html.unescape(achado.group(1)).strip()

    return ""


def encontrar_produto(objeto):
    if isinstance(objeto, list):
        for item in objeto:
            encontrado = encontrar_produto(item)

            if encontrado:
                return encontrado

    elif isinstance(objeto, dict):
        tipo = objeto.get("@type")

        if tipo == "Product":
            return objeto

        if isinstance(tipo, list) and "Product" in tipo:
            return objeto

        for valor in objeto.values():
            encontrado = encontrar_produto(valor)

            if encontrado:
                return encontrado

    return None


def extrair_dados_pagina(conteudo):
    produto_final = {
        "titulo": "",
        "imagem": "",
        "preco": None,
    }

    blocos = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>'
        r'(.*?)</script>',
        conteudo,
        re.S | re.I,
    )

    for bloco in blocos:
        try:
            dados = json.loads(bloco)
        except (ValueError, TypeError):
            continue

        produto = encontrar_produto(dados)

        if not produto:
            continue

        produto_final["titulo"] = (
            produto.get("name") or ""
        )

        imagem = produto.get("image")

        if isinstance(imagem, list):
            imagem = imagem[0] if imagem else ""

        if isinstance(imagem, dict):
            imagem = imagem.get("url", "")

        produto_final["imagem"] = imagem or ""

        ofertas = produto.get("offers", {})

        if isinstance(ofertas, list):
            ofertas = ofertas[0] if ofertas else {}

        if isinstance(ofertas, dict):
            for chave in ("price", "lowPrice"):
                preco = converter_preco(
                    ofertas.get(chave)
                )

                if preco is not None:
                    produto_final["preco"] = preco
                    break

        if produto_final["titulo"]:
            break

    if not produto_final["titulo"]:
        produto_final["titulo"] = obter_meta(
            conteudo, "og:title"
        )

    if not produto_final["imagem"]:
        produto_final["imagem"] = obter_meta(
            conteudo, "og:image"
        )

    # Primeiro tenta metadados específicos de preço.
    if produto_final["preco"] is None:
        for campo in (
            "product:price:amount",
            "og:price:amount",
            "product:price",
        ):
            valor = converter_preco(
                obter_meta(conteudo, campo)
            )

            if valor is not None:
                produto_final["preco"] = valor
                break

    # Depois tenta dados JSON incorporados na página.
    if produto_final["preco"] is None:
        produto_final["preco"] = extrair_preco_json(
            conteudo
        )

    produto_final["titulo"] = re.sub(
        r"\s*[|\-–]\s*Mercado Livre.*$",
        "",
        produto_final["titulo"],
        flags=re.I,
    ).strip()

    return produto_final


# ---------- API DO MERCADO LIVRE ----------

def extrair_id_item(url):
    encontrado = re.search(
        r"\b(MLB-?\d{6,})\b",
        url,
        re.I,
    )

    if encontrado:
        return encontrado.group(1).upper().replace("-", "")

    return None


def consultar_api_ml(url):
    item_id = extrair_id_item(url)

    if not item_id:
        print(
            "API: não encontrei o ID do anúncio "
            "na URL final."
        )
        return {}

    try:
        resposta = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=HEADERS,
            timeout=20,
        )

        print(
            "API do Mercado Livre: HTTP",
            resposta.status_code,
        )

        if resposta.status_code != 200:
            print("Resposta da API:", resposta.text[:300])
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

        print("Preço retornado pela API:", preco)

        return {
            "titulo": dados.get("title") or "",
            "imagem": imagem,
            "preco": preco,
        }

    except Exception as e:
        print("Erro ao consultar API:", repr(e))
        return {}


# ---------- BUSCAR PRODUTO ----------

def buscar_produto(url):
    try:
        resposta = requests.get(
            url,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"Falha ao abrir o link: {e}"
        )

    if resposta.status_code != 200:
        raise RuntimeError(
            f"A página retornou HTTP {resposta.status_code}."
        )

    url_final = resposta.url

    print("URL final do produto:", url_final)

    produto = extrair_dados_pagina(resposta.text)

    # A API recebe a URL final, após redirecionamentos.
    dados_api = consultar_api_ml(url_final)

    for campo in ("titulo", "imagem", "preco"):
        if produto.get(campo) in ("", None):
            valor = dados_api.get(campo)

            if valor not in ("", None):
                produto[campo] = valor

    if not produto["titulo"]:
        raise RuntimeError(
            "Não consegui identificar o título. "
            "A página pode estar bloqueada."
        )

    print("Título encontrado:", produto["titulo"])
    print("Preço final encontrado:", produto["preco"])

    return produto


# ---------- PUBLICAR NO GRUPO ----------

def montar_legenda(produto):
    titulo = html.escape(
        produto.get("titulo") or "Oferta"
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
            "💰 Não consegui confirmar o preço atual. "
            "Confira no botão Comprar.",
        ])

    return "\n".join(linhas)


def publicar(produto, link):
    legenda = montar_legenda(produto)

    botoes = json.dumps({
        "inline_keyboard": [[
            {
                "text": "🛒 Comprar",
                "url": link,
            }
        ]]
    })

    imagem = produto.get("imagem") or ""

    if imagem:
        resultado = telegram(
            "sendPhoto",
            chat_id=GROUP_ID,
            photo=imagem,
            caption=legenda,
            reply_markup=botoes,
        )

        if resultado.get("ok"):
            return True

        print(
            "Erro ao enviar foto:",
            resultado.get("description"),
        )

    resultado = telegram(
        "sendMessage",
        chat_id=GROUP_ID,
        text=legenda,
        reply_markup=botoes,
        disable_web_page_preview="true",
    )

    return resultado.get("ok", False)


# ---------- MENSAGENS RECEBIDAS ----------

def processar_mensagem(mensagem):
    chat = mensagem.get("chat", {})

    if chat.get("type") != "private":
        return

    usuario_id = mensagem.get("from", {}).get("id")
    texto = mensagem.get("text") or ""

    print("Mensagem recebida do ID:", usuario_id)

    # Sem IDs configurados: informa o ID ao usuário.
    if not ALLOWED:
        responder(
            chat["id"],
            f"Seu ID do Telegram é: {usuario_id}\n\n"
            "Adicione esse número ao segredo "
            "ALLOWED_USER_IDS no GitHub.",
        )
        return

    if usuario_id not in ALLOWED:
        responder(
            chat["id"],
            f"Seu ID do Telegram é: {usuario_id}\n"
            "Você ainda não está autorizado a usar o bot.",
        )
        return

    links = re.findall(
        r"https?://[^\s<>\"']+",
        texto,
    )

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

            if publicar(produto, link):
                responder(
                    chat["id"],
                    "✅ Produto publicado no grupo!",
                )
            else:
                responder(
                    chat["id"],
                    "❌ Não consegui publicar no grupo. "
                    "Confira as permissões do bot.",
                )

        except Exception as e:
            print("Erro ao processar produto:", repr(e))

            responder(
                chat["id"],
                "❌ Erro ao processar o produto:\n"
                + str(e)[:300],
            )

        time.sleep(2)


# ---------- EXECUTAR ----------

def main():
    if not TOKEN:
        print("ERRO: falta configurar TELEGRAM_TOKEN.")
        sys.exit(1)

    resultado = telegram("getMe")

    if not resultado.get("ok"):
        print(
            "Erro ao conectar ao Telegram:",
            resultado.get("description"),
        )
        sys.exit(1)

    bot = resultado.get("result", {})
    print("Bot conectado:", bot.get("username"))

    offset = None

    while True:
        parametros = {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": json.dumps(["message"]),
        }

        if offset is not None:
            parametros["offset"] = offset

        resultado = telegram("getUpdates", **parametros)

        if not resultado.get("ok"):
            print(
                "Erro ao buscar mensagens:",
                resultado.get("description"),
            )
            sys.exit(1)

        atualizacoes = resultado.get("result", [])

        if not atualizacoes:
            break

        for atualizacao in atualizacoes:
            offset = atualizacao["update_id"] + 1

            mensagem = atualizacao.get("message")

            if mensagem:
                try:
                    processar_mensagem(mensagem)
                except Exception as e:
                    print("Erro na mensagem:", repr(e))

    print("Execução concluída.")


if __name__ == "__main__":
    main()
