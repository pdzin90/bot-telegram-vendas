
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


def telegram(metodo, **dados):
    try:
        resposta = requests.post(
            f"{API}/{metodo}",
            data=dados,
            timeout=60,
        )
        resultado = resposta.json()

        if not resultado.get("ok"):
            print(
                f"Erro Telegram ({metodo}): "
                f"{resultado.get('description')}"
            )

        return resultado

    except Exception as erro:
        print(f"Erro de conexão com Telegram: {erro}")
        return {"ok": False, "description": str(erro)}


def responder(chat_id, texto):
    return telegram(
        "sendMessage",
        chat_id=chat_id,
        text=texto,
    )


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


def extrair_id_item(url):
    encontrado = re.search(
        r"\b(MLB-?\d{6,})\b",
        url,
        re.IGNORECASE,
    )

    if encontrado:
        return encontrado.group(1).upper().replace("-", "")

    return None


def consultar_api_ml(url):
    item_id = extrair_id_item(url)

    if not item_id:
        print("Não foi possível extrair o ID do anúncio.")
        return {}

    try:
        resposta = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=HEADERS,
            timeout=20,
        )

        if resposta.status_code != 200:
            print(
                "API Mercado Livre:",
                resposta.status_code,
                resposta.text[:200],
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

        return {
            "titulo": dados.get("title") or "",
            "imagem": imagem,
            "preco": converter_preco(dados.get("price")),
        }

    except Exception as erro:
        print("Erro ao consultar API:", erro)
        return {}


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
        resultado = re.search(padrao, conteudo, re.I)

        if resultado:
            return html.unescape(resultado.group(1)).strip()

    return ""


def encontrar_produto(objeto):
    if isinstance(objeto, list):
        for item in objeto:
            produto = encontrar_produto(item)
            if produto:
                return produto

    elif isinstance(objeto, dict):
        tipo = objeto.get("@type")

        if tipo == "Product":
            return objeto

        if isinstance(tipo, list) and "Product" in tipo:
            return objeto

        for valor in objeto.values():
            produto = encontrar_produto(valor)
            if produto:
                return produto

    return None


def extrair_dados(conteudo):
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
            objeto = json.loads(bloco)
            produto = encontrar_produto(objeto)
        except (ValueError, TypeError):
            continue

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
            produto_final["preco"] = converter_preco(
                ofertas.get("price")
                or ofertas.get("lowPrice")
            )

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

    if produto_final["preco"] is None:
        for campo in (
            "product:price:amount",
            "og:price:amount",
        ):
            produto_final["preco"] = converter_preco(
                obter_meta(conteudo, campo)
            )

            if produto_final["preco"] is not None:
                break

    return produto_final


def buscar_produto(url):
    resposta = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
        allow_redirects=True,
    )

    if resposta.status_code != 200:
        raise RuntimeError(
            f"O Mercado Livre respondeu HTTP "
            f"{resposta.status_code}."
        )

    produto = extrair_dados(resposta.text)

    dados_api = consultar_api_ml(resposta.url)

    for campo in ("titulo", "imagem", "preco"):
        if produto.get(campo) in ("", None):
            if dados_api.get(campo) not in ("", None):
                produto[campo] = dados_api[campo]

    if not produto["titulo"]:
        raise RuntimeError(
            "Não consegui encontrar o título do produto."
        )

    print(
        "Produto encontrado:",
        produto["titulo"],
        "| Preço:",
        produto["preco"],
    )

    return produto


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
            "💰 Consulte o preço atual no botão Comprar.",
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

        print("Erro ao enviar foto; tentando texto.")

    resultado = telegram(
        "sendMessage",
        chat_id=GROUP_ID,
        text=legenda,
        reply_markup=botoes,
        disable_web_page_preview="true",
    )

    return resultado.get("ok", False)


def processar_mensagem(mensagem):
    chat = mensagem.get("chat", {})

    if chat.get("type") != "private":
        return

    usuario_id = mensagem.get("from", {}).get("id")
    texto = mensagem.get("text") or ""

    print(f"Mensagem privada recebida do ID {usuario_id}")

    # Se não houver IDs autorizados, informa o ID no privado.
    if not ALLOWED:
        responder(
            chat["id"],
            f"Seu ID do Telegram é: {usuario_id}\n\n"
            "Adicione esse número ao segredo "
            "ALLOWED_USER_IDS no GitHub e execute novamente.",
        )
        return

    if usuario_id not in ALLOWED:
        responder(
            chat["id"],
            f"Seu ID do Telegram é: {usuario_id}\n"
            "Você ainda não está autorizado a usar este bot.",
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

        try:
            responder(
                chat["id"],
                "🔎 Buscando o produto...",
            )

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
                    "Envie somente links do Mercado Livre.",
                )
                continue

            produto = buscar_produto(link)

            if publicar(produto, link):
                responder(
                    chat["id"],
                    "✅ Oferta publicada no grupo!",
                )
            else:
                responder(
                    chat["id"],
                    "❌ Não consegui publicar no grupo. "
                    "Confira o ID e as permissões do bot.",
                )

        except Exception as erro:
            print("Erro ao processar produto:", repr(erro))
            responder(
                chat["id"],
                f"❌ Erro ao processar o produto:\n{str(erro)[:300]}",
            )

        time.sleep(2)


def main():
    if not TOKEN:
        print("ERRO: configure o segredo TELEGRAM_TOKEN.")
        sys.exit(1)

    resultado = telegram("getMe")

    if not resultado.get("ok"):
        print(
            "ERRO ao conectar ao Telegram:",
            resultado.get("description"),
        )
        sys.exit(1)

    bot = resultado.get("result", {})
    print(f"Bot conectado: @{bot.get('username', '')}")

    offset = None

    # Uma execução processa as mensagens pendentes e termina.
    while True:
        parametros = {
            "offset": offset,
            "timeout": 0,
            "limit": 100,
            "allowed_updates": json.dumps(["message"]),
        }

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
                except Exception as erro:
                    print("Erro na mensagem:", repr(erro))

    print("Processamento finalizado.")


if __name__ == "__main__":
    main()
