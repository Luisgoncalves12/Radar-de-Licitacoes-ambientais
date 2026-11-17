"""Argumentos de linha de comando e coordenacao da execucao."""

from contextlib import closing
from pathlib import Path
import argparse
import logging
import sqlite3

import requests

from . import auditoria as auditoria_mod
from . import banco as banco_mod
from . import coleta as coleta_mod
from . import configuracao as configuracao_mod
from . import planilhas as planilhas_mod


def main():
    parser = argparse.ArgumentParser(
        description="Coleta oportunidades do PNCP e exporta a planilha diária."
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument(
        "--nova-varredura",
        action="store_true",
        help="consulta novamente todas as combinações do dia",
    )
    modo.add_argument("--retomar", action="store_true", help="continua apenas uma rodada pendente")
    modo.add_argument("--somente-excel", action="store_true", help="exporta a última coleta salva")
    modo.add_argument(
        "--testar-conexao", action="store_true",
        help="faz uma consulta ao PNCP sem abrir o banco nem gerar planilha",
    )
    modo.add_argument(
        "--restaurar-marcacao", type=int, metavar="ID",
        help="reverte a última alteração de marcação de um edital pelo ID do histórico",
    )
    parser.add_argument("--limite-espera", type=float, default=60)
    parser.add_argument("--prazo-consulta", type=float, default=120)
    parser.add_argument("--prazo-total", type=float, default=configuracao_mod.PRAZO_TOTAL_PADRAO)
    parser.add_argument(
        "--tamanho-pagina", type=int, choices=range(10, 51), default=configuracao_mod.TAMANHO_PAGINA, metavar="10–50"
    )
    parser.add_argument("--importar-marcacoes", type=Path)
    parser.add_argument("--configuracao", type=Path, default=configuracao_mod.BASE_DIR / "configuracao.json")
    parser.add_argument("--validar-configuracao", action="store_true")
    argumentos = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
    )
    try:
        config = configuracao_mod.carregar_configuracao(argumentos.configuracao)
        coleta_mod._validar_limites(
            argumentos.limite_espera, argumentos.prazo_consulta, argumentos.prazo_total
        )
    except ValueError as erro:
        configuracao_mod.log.error("Erro de configuração: %s", erro)
        return 2
    if argumentos.validar_configuracao:
        print("Configuração válida")
        return 0
    if argumentos.testar_conexao:
        if argumentos.importar_marcacoes:
            parser.error("--testar-conexao e --importar-marcacoes devem ser executados separadamente")
        return coleta_mod.testar_conexao_pncp(config, argumentos.tamanho_pagina)
    if argumentos.restaurar_marcacao is not None and argumentos.importar_marcacoes:
        parser.error("--restaurar-marcacao e --importar-marcacoes devem ser executados separadamente")
    try:
        with banco_mod._bloquear_banco():
            return _executar(argumentos, config)
    except (ValueError, OSError, sqlite3.Error) as erro:
        configuracao_mod.log.error("Execução não concluída: %s", erro)
        return 1


def _executar(argumentos, config):
    if argumentos.restaurar_marcacao is not None:
        with closing(banco_mod.iniciar_banco()) as conn:
            numero = banco_mod.restaurar_marcacao(conn, argumentos.restaurar_marcacao)
        configuracao_mod.log.info("Marcação de %s restaurada no banco", numero)
        return 0
    if argumentos.somente_excel:
        resultado = banco_mod.ultima_coleta_salva()
    else:
        with closing(requests.Session()) as sessao:
            resultado = coleta_mod.coletar(
                dias_futuros=90,
                nova_varredura=argumentos.nova_varredura,
                somente_retomar=argumentos.retomar,
                limite_espera=argumentos.limite_espera,
                prazo_consulta=argumentos.prazo_consulta,
                prazo_total=argumentos.prazo_total,
                tamanho_pagina=argumentos.tamanho_pagina,
                config=config,
                sessao=sessao,
            )
    if argumentos.somente_excel:
        resultado["operacao"] = "Reexportação do banco, sem consulta ao PNCP"
    elif argumentos.retomar:
        resultado["operacao"] = "Retomada solicitada"
    elif argumentos.nova_varredura:
        resultado["operacao"] = "Nova varredura solicitada"
    else:
        resultado["operacao"] = "Coleta automática"
    estado = resultado["estado"]
    if estado == "sem_pendencias":
        configuracao_mod.log.info("Nenhuma rodada pendente para retomar.")
        if argumentos.importar_marcacoes:
            with closing(banco_mod.iniciar_banco()) as conn:
                quantidade = planilhas_mod.importar_marcacoes_captacao(
                    conn, argumentos.importar_marcacoes, config=config
                )
            configuracao_mod.log.info("Marcações importadas: %s", quantidade)
        auditoria_mod.publicar_resumo(resultado)
        return 0
    if estado == "falhou":
        auditoria_mod.publicar_resumo(resultado)
        return 1
    try:
        destino = planilhas_mod.exportar_excel(
            hoje=resultado["dia"], importar_de=argumentos.importar_marcacoes,
            config=config, coleta=resultado,
        )
    except (ValueError, OSError, sqlite3.Error):
        auditoria_mod.publicar_resumo(resultado)
        raise
    auditoria_mod.publicar_resumo(resultado, destino)
    return 2 if estado == "parcial" and not argumentos.somente_excel else 0
