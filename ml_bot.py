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
        r'"salePrice"\s*:\s*\{[^{}]*?"amount"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
        r'"retailPrice"\s*:\s*\{[^{}]*?"amount"\s*:\s*"?(\d+(?:[.,]\d+)?)"?',
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
        "preco_exato": False,
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
                    produto_final["preco_exato"] = True
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
                produto_final["preco_exato"] = True
                break

    # Depois tenta dados JSON incorporados na página.
    if produto_final["preco"] is None:
        produto_final["preco"] = extrair_preco_json(
            conteudo
        )

    produto_final["titulo"] = re.sub(
        r"\s*[|\-–]\s*(?:Mercado Livre|SHEIN|Shopee).*$",
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


# ---------- LOJAS (MERCADO LIVRE, SHOPEE, SHEIN) ----------

def dominio_em(host, dominios):
    return any(
        host == d or host.endswith("." + d)
        for d in dominios
    )


def identificar_loja(link):
    host = (
        requests.utils.urlparse(link).hostname or ""
    ).lower()

    if (
        "mercadolivre.com" in host
        or "mercadolibre.com" in host
        or host.endswith("meli.la")
    ):
        return "ml"

    if dominio_em(host, (
        "shopee.com.br",
        "shopee.com",
        "shope.ee",
        "shp.ee",
    )):
        return "shopee"

    if dominio_em(host, (
        "shein.com",
        "shein.com.br",
        "shein.top",
        "shein.onelink.me",
    )):
        return "shein"

    return None


NOMES_LOJAS = {
    "ml": "Mercado Livre",
    "shopee": "Shopee",
    "shein": "Shein",
}


# ---------- API DA SHOPEE ----------

def extrair_ids_shopee(*textos):
    padroes = (
        r"/product/(\d+)/(\d+)",
        r"-i\.(\d+)\.(\d+)",
        r"/opaanlp/(\d+)/(\d+)",
    )

    for texto in textos:
        if not texto:
            continue

        for padrao in padroes:
            achado = re.search(padrao, texto)

            if achado:
                return achado.group(1), achado.group(2)

    return None


def consultar_api_shopee(url_final, conteudo):
    ids = extrair_ids_shopee(
        url_final,
        obter_meta(conteudo, "og:url") if conteudo else "",
    )

    if not ids:
        print("Shopee: não achei shopid/itemid na URL final.")
        return {}

    shopid, itemid = ids

    try:
        cabecalhos = dict(HEADERS)
        cabecalhos.update({
            "Referer": url_final,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-API-SOURCE": "pc",
        })

        resposta = requests.get(
            "https://shopee.com.br/api/v4/item/get",
            params={"itemid": itemid, "shopid": shopid},
            headers=cabecalhos,
            timeout=20,
        )

        print("API da Shopee: HTTP", resposta.status_code)

        if resposta.status_code != 200:
            print("Resposta da API:", resposta.text[:300])
            return {}

        dados = resposta.json()

        if dados.get("error"):
            print("API da Shopee devolveu erro:", dados.get("error"))
            return {}

        item = dados.get("data") or dados.get("item") or {}

        if isinstance(item, dict) and isinstance(
            item.get("item"), dict
        ):
            item = item["item"]

        bruto = item.get("price") or item.get("price_min")
        preco = None

        if bruto:
            # a Shopee informa o preço multiplicado por 100000
            preco = converter_preco(float(bruto) / 100000)

        imagens = item.get("images") or []
        imagem = ""

        if imagens:
            imagem = (
                "https://down-br.img.susercontent.com/file/"
                f"{imagens[0]}"
            )

        print("Preço retornado pela Shopee:", preco)

        return {
            "titulo": item.get("name") or "",
            "imagem": imagem,
            "preco": preco,
        }

    except Exception as e:
        print("Erro ao consultar API da Shopee:", repr(e))
        return {}


# ---------- BUSCAR PRODUTO ----------

def titulo_generico(titulo, loja):
    t = (titulo or "").strip().lower()

    if not t:
        return True

    bloqueio = (
        "access denied",
        "captcha",
        "just a moment",
        "attention required",
        "verify you are human",
        "verificação de segurança",
    )

    if any(x in t for x in bloqueio):
        return True

    if loja == "shopee" and (
        "shopee brasil" in t or "ofertas incr" in t
    ):
        return True

    if loja == "shein" and t in (
        "shein",
        "shein br",
        "shein brasil",
    ):
        return True

    return False


def buscar_produto(url):
    loja = identificar_loja(url)

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

    url_final = resposta.url

    print("URL final do produto:", url_final)

    pagina = resposta.text if resposta.status_code == 200 else ""

    # A Shopee pode recusar a página e ainda assim responder à API.
    if resposta.status_code != 200 and loja != "shopee":
        aviso = ""

        if loja == "shein":
            aviso = " A Shein pode estar bloqueando acessos automáticos."

        raise RuntimeError(
            f"A página retornou HTTP {resposta.status_code}.{aviso}"
        )

    if pagina:
        produto = extrair_dados_pagina(pagina)
    else:
        produto = {
            "titulo": "",
            "imagem": "",
            "preco": None,
            "preco_exato": False,
        }

    # Página de bloqueio ou "casca" sem dados do produto.
    if loja != "ml" and titulo_generico(produto["titulo"], loja):
        produto["titulo"] = ""
        produto["imagem"] = ""
        produto["preco"] = None
        produto["preco_exato"] = False

    if loja == "ml":
        # A API recebe a URL final, após redirecionamentos.
        dados_api = consultar_api_ml(url_final)
    elif loja == "shopee":
        dados_api = consultar_api_shopee(url_final, pagina)
    else:
        dados_api = {}

    # Na Shopee a página é montada por JavaScript, então a API manda.
    api_manda = loja == "shopee"

    for campo in ("titulo", "imagem", "preco"):
        atual = produto.get(campo)
        novo = dados_api.get(campo)

        if novo in ("", None):
            continue

        if api_manda or atual in ("", None):
            produto[campo] = novo

            if campo == "preco":
                produto["preco_exato"] = True

    if not produto["titulo"]:
        if loja in ("shopee", "shein"):
            raise RuntimeError(
                f"Não consegui ler o produto. A {NOMES_LOJAS[loja]} "
                "costuma bloquear a leitura automática."
            )

        raise RuntimeError(
            "Não consegui identificar o título. "
            "A página pode estar bloqueada."
        )

    produto["loja"] = loja

    print("Loja:", loja)
    print("Título encontrado:", produto["titulo"])
    print("Preço final encontrado:", produto["preco"])

    return produto


# ---------- PUBLICAR NO GRUPO ----------

def montar_legenda(produto):
    # Sem parse_mode no Telegram: não escapar o texto, senão "&"
    # apareceria como "&amp;".
    titulo = produto.get("titulo") or "Oferta"

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
            caption=legenda[:1024],
            reply_markup=botoes,
        )

        if resultado.get("ok"):
            return True

        print(
            "Erro ao enviar foto:",
            resultado.get("description"),
        )

    texto = legenda
    previa = "true"

    # Postagem manual (página não lida): deixa o Telegram tentar
    # montar a prévia com a foto a partir do link.
    if produto.get("manual"):
        texto = f"{legenda}\n\n{link}"
        previa = "false"

    resultado = telegram(
        "sendMessage",
        chat_id=GROUP_ID,
        text=texto[:4096],
        reply_markup=botoes,
        disable_web_page_preview=previa,
    )

    return resultado.get("ok", False)


# ---------- MENSAGENS RECEBIDAS ----------

REGEX_LINK = r"https?://[^\s<>\"']+"
REGEX_PRECO_MANUAL = r"R\$\s*(\d[\d.]*(?:,\d{1,2})?)"


def extrair_preco_manual(texto):
    achado = re.search(REGEX_PRECO_MANUAL, texto or "")

    if not achado:
        return None

    bruto = achado.group(1)

    # "1.299" sem vírgula são milhares, não decimais.
    if "," not in bruto and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", bruto):
        bruto = bruto.replace(".", "")

    return converter_preco(bruto)


def extrair_titulo_manual(texto):
    sem_preco = re.sub(REGEX_PRECO_MANUAL, "", texto or "")

    for linha in sem_preco.splitlines():
        linha = linha.strip(" -–|:")

        if linha:
            return linha[:200]

    return ""


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

    links = re.findall(REGEX_LINK, texto)

    if not links:
        responder(
            chat["id"],
            "Envie um link de produto do Mercado Livre, "
            "Shopee ou Shein.",
        )
        return

    # Texto extra só vale quando a mensagem tem um único link.
    extra = ""

    if len(links) == 1:
        extra = re.sub(REGEX_LINK, "", texto).strip()

    preco_manual = extrair_preco_manual(extra)
    titulo_manual = extrair_titulo_manual(extra)

    for link in links:
        link = link.rstrip(").,;")
        loja = identificar_loja(link)

        if not loja:
            responder(
                chat["id"],
                "Este bot aceita links do Mercado Livre, "
                "Shopee e Shein.",
            )
            continue

        try:
            responder(
                chat["id"],
                "🔎 Buscando o produto e o preço atual...",
            )

            try:
                produto = buscar_produto(link)

            except Exception as erro_leitura:
                # Plano B: você mandou título e preço junto com o link.
                if (
                    loja != "ml"
                    and titulo_manual
                    and preco_manual is not None
                ):
                    print(
                        "Leitura falhou, usando dados manuais:",
                        repr(erro_leitura),
                    )

                    produto = {
                        "titulo": titulo_manual,
                        "imagem": "",
                        "preco": preco_manual,
                        "preco_exato": True,
                        "manual": True,
                        "loja": loja,
                    }

                    responder(
                        chat["id"],
                        "⚠️ Não consegui ler a página. Vou postar "
                        "com o título e o preço que você enviou.",
                    )
                else:
                    raise

            else:
                # "R$ 49,90" escrito por você sempre vence a leitura.
                if preco_manual is not None:
                    produto["preco"] = preco_manual
                    produto["preco_exato"] = True

            if publicar(produto, link):
                aviso = ""

                if loja != "ml" and produto.get("preco") is None:
                    aviso = (
                        "\n⚠️ Publicado sem preço. Para incluir, "
                        "envie o link com 'R$ 49,90' na mesma mensagem."
                    )
                elif loja != "ml" and not produto.get("preco_exato"):
                    aviso = (
                        "\n⚠️ O preço foi lido de forma aproximada "
                        "da página. Confira no grupo."
                    )

                responder(
                    chat["id"],
                    "✅ Produto publicado no grupo!" + aviso,
                )
            else:
                responder(
                    chat["id"],
                    "❌ Não consegui publicar no grupo. "
                    "Confira as permissões do bot.",
                )

        except Exception as e:
            print("Erro ao processar produto:", repr(e))

            dica = ""

            if loja != "ml":
                dica = (
                    "\n\nDica: envie o título e o preço junto com "
                    "o link, assim:\n"
                    "Nome do produto R$ 49,90\n"
                    "https://link..."
                )

            responder(
                chat["id"],
                "❌ Erro ao processar o produto:\n"
                + str(e)[:300]
                + dica,
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
