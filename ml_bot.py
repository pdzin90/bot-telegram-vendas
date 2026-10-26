"""
Bot de ofertas do Mercado Livre para Telegram.
Recebe links no privado e publica no grupo com foto,
título, preço antigo quando disponível, preço atual e botão.
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
    "Accept": "text/html,application/xhtml+xml",
}

URL_RE = re.compile(r"https?://[^\s<>\"']+")


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
    resultado = tg(
        "sendMessage",
        chat_id=chat_id,
        text=texto,
        disable_web_page_preview="true",
    )

    if not resultado.get("ok"):
        print("Erro ao enviar mensagem privada:", resultado)


# ---------------- EXTRAÇÃO DE DADOS ----------------

def meta(texto, propriedade):
    padroes = [
        rf'<meta[^>]+(?:property|name|itemprop)='
        rf'["\']{re.escape(propriedade)}["\'][^>]*'
        rf'content=["\']([^"\']*)["\']',

        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*'
        rf'(?:property|name|itemprop)='
        rf'["\']{re.escape(propriedade)}["\']',
    ]

    for padrao in padroes:
        encontrado = re.search(padrao, texto, re.I)
        if encontrado:
            return html.unescape(encontrado.group(1)).strip()

    return ""


def achar_produto(objeto):
    if isinstance(objeto, list):
        for item in objeto:
            resultado = achar_produto(item)
            if resultado:
                return resultado

    elif isinstance(objeto, dict):
        tipo = objeto.get("@type")

        if tipo == "Product":
            return objeto

        if isinstance(tipo, list) and "Product" in tipo:
            return objeto

        for valor in objeto.values():
            resultado = achar_produto(valor)
            if resultado:
                return resultado

    return None


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

    except (TypeError, ValueError):
        return None


def preco_das_ofertas(ofertas):
    if isinstance(ofertas, list):
        ofertas = ofertas[0] if ofertas else {}

    if not isinstance(ofertas, dict):
        return None

    for chave in ("price", "lowPrice"):
        valor = converter_preco(ofertas.get(chave))
        if valor is not None:
            return valor

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


def extrair_produto(texto_html):
    titulo = ""
    imagem = ""
    preco = None
    preco_antigo = None

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
        except Exception:
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

        for chave in (
            "listPrice",
            "originalPrice",
            "original_price",
        ):
            valor = converter_preco(produto.get(chave))

            if (
                valor is not None
                and (preco is None or valor > preco)
            ):
                preco_antigo = valor
                break

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

    for propriedade in (
        "product:original_price:amount",
        "og:price:standard_amount",
        "product:price:original_amount",
    ):
        valor = converter_preco(
            meta(texto_html, propriedade)
        )

        if (
            valor is not None
            and (preco is None or valor > preco)
        ):
            preco_antigo = preco_antigo or valor
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
        "preco_antigo": preco_antigo,
    }


# ---------------- API DO MERCADO LIVRE ----------------

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
        return {}

    try:
        resposta = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=HEADERS,
            timeout=20,
        )

        if resposta.status_code != 200:
            print(
                "API do Mercado Livre retornou:",
                resposta.status_code,
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
            "titulo": str(dados.get("title") or "").strip(),
            "imagem": imagem,
            "preco": converter_preco(dados.get("price")),
            "preco_antigo": converter_preco(
                dados.get("original_price")
            ),
        }

    except Exception as exc:
        print("Erro ao consultar API do Mercado Livre:", exc)
        return {}


def identificar_loja(url):
    host = (
        requests.utils.urlparse(url).hostname or ""
    ).lower()

    host = host.removeprefix("www.")

    if any(
        dominio in host
        for dominio in (
            "mercadolivre.com",
            "mercadolibre.com",
            "meli.la",
        )
    ):
        return "Mercado Livre"

    return "outra loja"


def buscar_produto(url):
    if identificar_loja(url) != "Mercado Livre":
        raise RuntimeError(
            "Envie um link do Mercado Livre."
        )

    resposta = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
        allow_redirects=True,
    )

    if resposta.status_code != 200:
        raise RuntimeError(
            f"A página respondeu HTTP {resposta.status_code}. "
            "O Mercado Livre pode ter bloqueado a consulta."
        )

    produto = extrair_produto(resposta.text)
    produto["url_final"] = resposta.url
    produto["loja"] = "Mercado Livre"

    api_produto = consultar_api_ml(resposta.url)

    for campo in (
        "titulo",
        "imagem",
        "preco",
        "preco_antigo",
    ):
        if produto.get(campo) in ("", None):
            valor_api = api_produto.get(campo)

            if valor_api not in ("", None):
                produto[campo] = valor_api

    if not produto.get("titulo"):
        raise RuntimeError(
            "Não consegui encontrar o título do produto. "
            "A página pode estar bloqueada ou exigir JavaScript."
        )

    preco = produto.get("preco")
    antigo = produto.get("preco_antigo")

    # Só mostra preço antigo se ele for maior que o atual.
    if preco is not None and antigo is not None:
        if antigo <= preco:
            produto["preco_antigo"] = None

    if produto.get("preco") is None:
        print("Aviso: não encontrei o preço atual.")

    if produto.get("preco_antigo") is None:
        print("Aviso: não encontrei o preço antigo.")

    return produto


# ---------------- PUBLICAÇÃO NO GRUPO ----------------

def montar_legenda(produto, extra=""):
    titulo = html.escape(produto["titulo"])
    linhas = [f"🔥 {titulo}"]

    preco = produto.get("preco")
    antigo = produto.get("preco_antigo")

    if preco is not None:
        if antigo is not None and antigo > preco:
            linhas.extend([
                "",
                f"<s>De {formatar_preco(antigo)}</s>",
                f"💥 POR {formatar_preco(preco)}!",
            ])
        else:
            linhas.extend([
                "",
                f"💰 Preço atual: {formatar_preco(preco)}",
            ])
            linhas.append(
                "ℹ️ Preço antigo não disponível."
            )
    else:
        linhas.extend([
            "",
            "💰 Consulte o preço atualizado no botão Comprar.",
        ])

    if extra:
        linhas.extend(["", html.escape(extra)])

    return "\n".join(linhas)


def postar(produto, link, extra=""):
    legenda = montar_legenda(produto, extra)

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
            caption=legenda[:1024],
            parse_mode="HTML",
            reply_markup=botao,
        )

        if resultado.get("ok"):
            return True, ""

        print(
            "Não consegui enviar a foto:",
            resultado.get("description"),
        )

    resultado = tg(
        "sendMessage",
        chat_id=GROUP_ID,
        text=legenda[:4096],
        parse_mode="HTML",
        reply_markup=botao,
        disable_web_page_preview="true",
    )

    return (
        resultado.get("ok", False),
        resultado.get("description", ""),
    )


# ---------------- MENSAGENS RECEBIDAS ----------------

def tratar_mensagem(msg):
    chat = msg.get("chat", {})

    if chat.get("type") != "private":
        return

    uid = msg.get("from", {}).get("id")
    texto = msg.get("text") or msg.get("caption") or ""

    print(
        f"Mensagem privada recebida. "
        f"ID: {uid}; contém texto: {bool(texto)}"
    )

    if uid not in ALLOWED:
        if ALLOWED:
            responder(
                chat["id"],
                "Você não tem permissão para usar este bot.",
            )
        else:
            responder(
                chat["id"],
                f"Seu ID do Telegram é: {uid}\n"
                "Adicione esse número ao segredo "
                "ALLOWED_USER_IDS no GitHub.",
            )
        return

    links = URL_RE.findall(texto)

    if not links:
        responder(
            chat["id"],
            "Envie um link de produto do Mercado Livre.",
        )
        return

    extra = URL_RE.sub("", texto).strip() if len(links) == 1 else ""

    for link in links:
        link = link.rstrip(").,;")

        try:
            produto = buscar_produto(link)
            sucesso, erro = postar(produto, link, extra)

            if sucesso:
                responder(
                    chat["id"],
                    "✅ Produto publicado no grupo!\n"
                    f"{produto['titulo'][:100]}",
                )
            else:
                responder(
                    chat["id"],
                    "❌ Não consegui publicar no grupo.\n"
                    f"Motivo: {erro}",
                )

        except Exception as exc:
            print("Erro ao processar link:", repr(exc))
            responder(
                chat["id"],
                "❌ Não consegui ler esse produto.\n"
                f"Motivo: {str(exc)[:300]}\n\n"
                f"Link: {link}",
            )

        time.sleep(2)


# ---------------- PROCESSAMENTO DO TELEGRAM ----------------

def processar(offset=None, espera=0):
    total = 0

    while True:
        parametros = {
            "timeout": espera if total == 0 else 0,
            "allowed_updates": json.dumps(["message"]),
            "limit": 100,
        }

        if offset is not None:
            parametros["offset"] = offset

        print("Consultando mensagens pendentes...")
        resultado = tg("getUpdates", **parametros)

        if not resultado.get("ok"):
            print(
                "Erro ao buscar mensagens:",
                resultado.get("description"),
            )
            return offset

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
                    print("Erro ao tratar mensagem:", repr(exc))

    print(f"Processamento concluído: {total} atualização(ões).")
    return offset


def main():
    if not TOKEN:
        print("ERRO: configure o segredo TELEGRAM_TOKEN.")
        sys.exit(1)

    print("Verificando o bot do Telegram...")
    resultado = tg("getMe")

    if not resultado.get("ok"):
        print(
            "ERRO: token inválido ou problema de conexão. "
            "Confira TELEGRAM_TOKEN."
        )
        sys.exit(1)

    bot = resultado.get("result", {})
    print(f"Bot conectado: @{bot.get('username', 'desconhecido')}")

    if "--loop" in sys.argv:
        offset = None

        while True:
            offset = processar(offset, espera=50)
    else:
        processar()


if __name__ == "__main__":
    main()
