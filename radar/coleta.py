"""Consultas HTTP, paginacao e retomada da coleta PNCP."""

from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
import hashlib
import json
import math
import random
import time

import pandas as pd
import requests

from . import banco as banco_mod
from . import classificacao as classificacao_mod
from . import configuracao as configuracao_mod
from . import dados as dados_mod


def _validar_limites(*valores):
    if any(
        not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
        for v in valores
    ):
        raise ValueError("Os limites de tempo devem ser finitos e positivos")


def segundos_retry_after(valor, agora=None):
    if valor is None:
        return None
    valor = str(valor).strip()
    if valor.isdecimal():
        return int(valor)
    try:
        data = parsedate_to_datetime(valor)
        if data.tzinfo is None:
            data = data.replace(tzinfo=timezone.utc)
        return max(0.0, (data - (agora or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def consultar_pagina(
    pagina,
    codigo_modalidade,
    uf,
    data_inicial,
    data_final,
    tentativas=6,
    timeout=20,
    metricas=None,
    sessao=None,
    limite_espera=60,
    prazo_consulta=120,
    prazo_execucao=None,
    tamanho_pagina=configuracao_mod.TAMANHO_PAGINA,
):
    propria = sessao is None
    sessao = sessao if sessao is not None else requests.Session()
    try:
        params = {
            "dataInicial": data_inicial,
            "dataFinal": data_final,
            "pagina": pagina,
            "codigoModalidadeContratacao": codigo_modalidade,
            "uf": uf,
            "tamanhoPagina": tamanho_pagina,
        }
        _validar_limites(timeout, limite_espera, prazo_consulta)
        if prazo_execucao is not None and not math.isfinite(prazo_execucao):
            raise ValueError("O prazo absoluto deve ser finito")
        prazo = min(
            time.monotonic() + prazo_consulta,
            prazo_execucao if prazo_execucao is not None else float("inf"),
        )
        for tentativa in range(tentativas):
            restante = prazo - time.monotonic()
            if restante <= 0:
                break
            resp = None
            espera_servidor = None
            try:
                inicio_requisicao = time.monotonic()
                try:
                    resp = sessao.get(configuracao_mod.URL, params=params, timeout=min(timeout, restante))
                finally:
                    if metricas is not None:
                        metricas["chamadas"] += 1
                        metricas["resposta_s"] += time.monotonic() - inicio_requisicao
                if resp.status_code == 429:
                    espera_servidor = segundos_retry_after(resp.headers.get("Retry-After"))
                    configuracao_mod.log.warning(
                        "PNCP limitou requisições (HTTP 429) em %s/modalidade %s/página %s "
                        "(tentativa %s/%s; Retry-After: %s)",
                        uf, codigo_modalidade, pagina, tentativa + 1, tentativas,
                        resp.headers.get("Retry-After", "ausente"),
                    )
                    if espera_servidor is not None and espera_servidor > limite_espera:
                        configuracao_mod.log.error("Espera solicitada pelo servidor excede o teto; consulta adiada")
                        break
                elif resp.status_code == 204:
                    return None
                elif resp.status_code == 400:
                    configuracao_mod.log.error("PNCP rejeitou a consulta (HTTP 400) em %s/modalidade %s/página %s "
                              "com tamanhoPagina=%s", uf, codigo_modalidade, pagina, tamanho_pagina)
                    break
                else:
                    resp.raise_for_status()
                    dados = resp.json()
                    if time.monotonic() >= prazo:
                        break
                    return dados
            except requests.exceptions.ProxyError as erro:
                configuracao_mod.log.warning("Falha de proxy em %s/modalidade %s/página %s: %s",
                            uf, codigo_modalidade, pagina, erro)
                break
            except requests.exceptions.Timeout:
                configuracao_mod.log.warning("Timeout em %s/modalidade %s/página %s", uf, codigo_modalidade, pagina)
            except requests.exceptions.RequestException as erro:
                configuracao_mod.log.warning(
                    f"Erro de requisição ({uf}, modalidade {codigo_modalidade}, pág {pagina}): {erro}"
                )
            finally:
                if resp is not None:
                    resp.close()
            if tentativa + 1 >= tentativas:
                break
            base = min(limite_espera, 2 ** min(tentativa, 10))
            espera = min(limite_espera, base + random.uniform(0, base / 4))
            if espera_servidor is not None:
                espera = max(espera, espera_servidor)
            restante = prazo - time.monotonic()
            if restante <= espera:
                break
            if metricas is not None and resp is not None and resp.status_code == 429:
                metricas["espera_429_s"] += espera
            time.sleep(espera)
        configuracao_mod.log.error("Consulta não concluída em %s/modalidade %s/página %s", uf, codigo_modalidade, pagina)
        return "FALHOU"
    finally:
        if propria:
            sessao.close()


def validar_pagina(resultado, pagina):
    if resultado is None:
        if pagina == 1:
            return None
        raise ValueError("Resposta sem conteúdo depois da primeira página")
    if not isinstance(resultado, dict) or not isinstance(resultado.get("data"), list):
        raise ValueError("Resposta deve conter uma lista data")
    total = resultado.get("totalPaginas")
    if type(total) is not int or total < 0:
        raise ValueError("totalPaginas ausente ou inválido")
    if resultado["data"] and total < pagina:
        raise ValueError("Paginação incompatível com os dados")
    total_registros = resultado.get("totalRegistros")
    if total_registros is not None and (
        type(total_registros) is not int or total_registros < len(resultado["data"])
    ):
        raise ValueError("Total de registros incompatível com os dados")
    if not resultado["data"] and (total > 0 or pagina > 1 or total_registros):
        raise ValueError("Página vazia apesar de haver resultados anunciados")
    for item in resultado["data"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("numeroControlePNCP"), str)
            or not item["numeroControlePNCP"].strip()
        ):
            raise ValueError("Item sem número de controle válido")
        for campo in ("unidadeOrgao", "orgaoEntidade"):
            if item.get(campo) is not None and not isinstance(item[campo], dict):
                raise ValueError(f"{campo} deve ser um objeto")
        modalidade = item.get("modalidadeId")
        if modalidade is not None and (type(modalidade) is not int or modalidade <= 0):
            raise ValueError("modalidadeId deve ser um inteiro positivo")
        for campos, dados in (
            (("objetoCompra", "informacaoComplementar", "dataAberturaProposta",
              "dataEncerramentoProposta", "situacaoCompraNome", "linkSistemaOrigem",
              "dataAtualizacao"), item),
            (("ufSigla", "municipioNome", "nomeUnidade"), item.get("unidadeOrgao") or {}),
            (("razaoSocial",), item.get("orgaoEntidade") or {}),
        ):
            for campo in campos:
                if dados.get(campo) is not None and not isinstance(dados[campo], str):
                    raise ValueError(f"{campo} deve ser texto")
    return resultado


def assinatura_configuracao(inicio=None, fim=None, config=None):
    parametros = {
        **configuracao_mod._configuracao(config),
        "versao_regras": configuracao_mod.VERSAO_REGRAS,
        "locais_bloqueados": sorted(configuracao_mod.LOCAIS_BLOQUEADOS),
        "variantes": configuracao_mod.VARIANTES_TERMOS,
        "inicio": str(inicio),
        "fim": str(fim),
    }
    return hashlib.sha256(
        json.dumps(parametros, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def atualizar_situacoes(conn, sessao, rodada_id, hoje, fim, prazo, delay, config, acompanhadas=False):
    if acompanhadas:
        candidatos = conn.execute(
            """SELECT numeroControlePNCP FROM licitacoes WHERE jaCaptou=1
               ORDER BY COALESCE(dataTentativaSituacao,''), numeroControlePNCP"""
        ).fetchall()
    else:
        candidatos = conn.execute(
            """SELECT numeroControlePNCP FROM licitacoes
               WHERE COALESCE(descartada,0)=0 AND COALESCE(jaCaptou,0)<>1
                 AND COALESCE(ultimaRodada,'')<>?
                 AND substr(dataEncerramentoProposta,1,10) BETWEEN ? AND ?
                 AND uf IN (SELECT value FROM json_each(?))
                 AND modalidadeId IN (SELECT value FROM json_each(?))""",
            (rodada_id, hoje.isoformat(), fim.isoformat(),
             json.dumps(config["ufs"]), json.dumps(config["modalidades"])),
        ).fetchall()
    chamadas = atualizadas = 0
    for (numero,) in candidatos:
        restante = prazo - time.monotonic()
        if restante <= 0:
            configuracao_mod.log.warning("Prazo esgotado ao conferir a situação de editais")
            break
        edital = dados_mod._identificar_edital(numero)
        if not edital:
            continue
        consultado_em = configuracao_mod.agora_brasilia().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE licitacoes SET dataTentativaSituacao=? WHERE numeroControlePNCP=?",
            (consultado_em, numero),
        )
        conn.commit()
        chamadas += 1
        try:
            with sessao.get(configuracao_mod.URL_DETALHE.format(**edital), timeout=min(20, restante)) as resposta:
                if resposta.status_code == 429:
                    configuracao_mod.log.warning("PNCP limitou as consultas individuais de situação; conferência adiada")
                    break
                resposta.raise_for_status()
                dados = resposta.json()
            if not isinstance(dados, dict) or dados.get("numeroControlePNCP") != numero:
                raise ValueError("O detalhe não corresponde ao edital solicitado")
            data_atualizacao = dados.get("dataAtualizacao")
            if data_atualizacao is not None and not isinstance(data_atualizacao, str):
                raise ValueError("dataAtualizacao do detalhe deve ser texto")
            situacao = dados.get("situacaoCompraNome")
            if not isinstance(situacao, str) or not situacao.strip():
                raise ValueError("O detalhe não contém uma situação válida")
        except (requests.exceptions.RequestException, ValueError) as erro:
            configuracao_mod.log.warning("Situação de %s não confirmada: %s", numero, erro)
        else:
            atual = banco_mod._buscar_licitacao(conn, numero)
            mesclado = banco_mod._item_mesclado(atual, {
                "situacaoCompraNome": situacao, "dataAtualizacao": data_atualizacao,
            })
            if mesclado["situacaoCompraNome"] == situacao:
                mudou = situacao != atual["situacao"]
                movimentacao = dados.get("dataAtualizacaoGlobal") or data_atualizacao
                data_movimento = dados_mod._timestamp_brasilia(movimentacao) if isinstance(movimentacao, str) else pd.NaT
                anterior = dados_mod._timestamp_brasilia(atual["dataMovimentacaoPNCP"])
                if pd.isna(data_movimento) or data_movimento < anterior:
                    movimentacao = None
                conn.execute(
                    """UPDATE licitacoes SET situacao=?,dadosCompletos=?,dataAtualizacaoPNCP=?,
                       dataConsultaSituacao=?,dataMovimentacaoPNCP=COALESCE(?,dataMovimentacaoPNCP),
                       ultimaAlteracao=COALESCE(?,ultimaAlteracao) WHERE numeroControlePNCP=?""",
                    (situacao, json.dumps(mesclado, ensure_ascii=False), mesclado.get("dataAtualizacao"),
                     consultado_em, movimentacao, hoje.isoformat() if mudou else None, numero),
                )
                if mudou:
                    conn.execute(
                        """INSERT INTO historico_alteracoes
                           (numeroControlePNCP,data,campo,valorAntigo,valorNovo)
                           VALUES (?,?,'situacao',?,?)""",
                        (numero, hoje.isoformat(), atual["situacao"], situacao),
                    )
                    atualizadas += 1
            else:
                configuracao_mod.log.warning("Situação de %s mantida: o detalhe não confirma uma versão mais recente", numero)
            conn.commit()
        time.sleep(min(delay, max(0, prazo - time.monotonic())))
    return chamadas, atualizadas


def coletar(
    dias_futuros=90,
    delay=None,
    nova_varredura=False,
    prazo_total=configuracao_mod.PRAZO_TOTAL_PADRAO,
    prazo_consulta=120,
    limite_espera=60,
    config=None,
    sessao=None,
    db_path=None,
    somente_retomar=False,
    tamanho_pagina=configuracao_mod.TAMANHO_PAGINA,
):
    config = configuracao_mod._configuracao(config)
    _validar_limites(prazo_total, prazo_consulta, limite_espera)
    if type(tamanho_pagina) is not int or not 10 <= tamanho_pagina <= 50:
        raise ValueError("O tamanho da página deve ser um inteiro entre 10 e 50")
    ufs, modalidades = config["ufs"], config["modalidades"]
    total_combinacoes = len(set(ufs)) * len(set(modalidades))
    with banco_mod._bloquear_banco(db_path), (
        closing(requests.Session()) if sessao is None else nullcontext(sessao)
    ) as sessao:
        prazo_execucao = time.monotonic() + prazo_total
        delay = configuracao_mod.DELAY_ENTRE_REQUISICOES if delay is None else delay
        agora = configuracao_mod.agora_brasilia()
        hoje = agora.date()
        dia = hoje.isoformat()
        data_final = hoje + timedelta(days=dias_futuros)
        conn = banco_mod.iniciar_banco(db_path)
        try:
            assinatura = assinatura_configuracao(hoje, data_final, config=config)
            rodada_id, assinatura_anterior, estado_anterior = banco_mod._rodada_do_dia(conn, dia)
            if somente_retomar and estado_anterior in (None, "completa"):
                return {"estado": "sem_pendencias", "dia": hoje, "chamadas": 0, "pendentes": None}
            if (
                not nova_varredura and estado_anterior not in (None, "completa")
                and assinatura_anterior != assinatura
            ):
                raise ValueError(
                    "A rodada pendente usa outra configuração ou janela. "
                    "Use os parâmetros originais para retomar ou solicite --nova-varredura."
                )
            feitos = set()
            if nova_varredura or assinatura_anterior != assinatura or estado_anterior == "completa":
                rodada_id = uuid4().hex
                conn.execute("DELETE FROM progresso WHERE dia=?", (dia,))
                conn.execute("DELETE FROM paginas_coleta WHERE dia=?", (dia,))
            else:
                feitos.update(
                    conn.execute(
                        "SELECT uf,modalidade FROM progresso WHERE dia=? AND assinatura=? AND rodada_id=?",
                        (dia, assinatura, rodada_id),
                    )
                )
            tentativa_id = conn.execute(
                """INSERT INTO log_execucoes
                   (timestamp,estado,rodada_id,dia_coleta,assinatura,total_combinacoes)
                   VALUES (?,'em_andamento',?,?,?,?)""",
                (agora.isoformat(timespec="seconds"), rodada_id, dia, assinatura, total_combinacoes),
            ).lastrowid
            # A rodada precisa estar visível antes do primeiro commit de dados ou checkpoints.
            conn.commit()
            banco_mod.reclassificar_banco(conn, config=config, dia_coleta=hoje)
            prazo_acompanhamento = min(prazo_execucao, time.monotonic() + min(180, prazo_total / 10))
            chamadas, atualizadas = atualizar_situacoes(
                conn, sessao, rodada_id, hoje, data_final, prazo_acompanhamento, delay, config,
                acompanhadas=True,
            )
            conn.commit()
            total_escaneados = total_falhas = 0
            novas = descartadas = 0
            vistos = {}
            relevantes = set()
            existentes = {r[0] for r in conn.execute("SELECT numeroControlePNCP FROM licitacoes")}
            interrompido = False
            consultas_ok = 0

            configuracao_mod.log.info(f"Buscando propostas abertas de {hoje:%d/%m/%Y} até {data_final:%d/%m/%Y}.")
            if feitos:
                configuracao_mod.log.info(
                    f"Retomando: {len(feitos)} de {total_combinacoes} combinações já coletadas hoje."
                )

            for uf in ufs:
                if interrompido:
                    break
                metricas_uf = {
                    "chamadas": 0,
                    "resposta_s": 0.0,
                    "espera_429_s": 0.0,
                    "pausa_s": 0.0,
                }
                inicio_uf = time.monotonic()
                escaneados_antes = total_escaneados
                for modalidade in modalidades:
                    if (uf, modalidade) in feitos:
                        continue
                    # O PNCP pagina por número. Itens podem mudar de página entre execuções.
                    pagina, combo_ok = 1, True
                    numeros_combinacao = set()
                    while True:
                        if time.monotonic() >= prazo_execucao:
                            configuracao_mod.log.warning(
                                "Prazo total de %.0fs esgotado em %s/modalidade %s/página %s; "
                                "a próxima execução retomará as combinações pendentes.",
                                prazo_total, uf, modalidade, pagina,
                            )
                            total_falhas += 1
                            interrompido = True
                            combo_ok = False
                            break
                        resultado = consultar_pagina(
                            pagina,
                            modalidade,
                            uf,
                            hoje.strftime("%Y%m%d"),
                            data_final.strftime("%Y%m%d"),
                            metricas=metricas_uf,
                            prazo_execucao=prazo_execucao,
                            prazo_consulta=prazo_consulta,
                            limite_espera=limite_espera,
                            sessao=sessao,
                            tamanho_pagina=tamanho_pagina,
                        )
                        metricas_uf["pausa_s"] += delay
                        time.sleep(delay)
                        if resultado != "FALHOU":
                            try:
                                resultado = validar_pagina(resultado, pagina)
                            except ValueError as erro:
                                configuracao_mod.log.error(
                                    "Resposta inválida em %s/%s/página %s: %s",
                                    uf,
                                    modalidade,
                                    pagina,
                                    erro,
                                )
                                resultado = "FALHOU"
                        if resultado == "FALHOU":
                            total_falhas += 1
                            combo_ok = False
                            break
                        consultas_ok += 1
                        if resultado is None or not resultado.get("data"):
                            break
                        total_escaneados += len(resultado["data"])
                        for item in resultado["data"]:
                            numero = item["numeroControlePNCP"]
                            numeros_combinacao.add(numero)
                            anterior_na_rodada = vistos.get(numero)
                            if anterior_na_rodada == item:
                                continue
                            item = banco_mod._mesclar_versoes(anterior_na_rodada or {}, item)
                            vistos[numero] = item
                            atual = (
                                banco_mod._buscar_licitacao(conn, numero) if numero in existentes else None
                            )
                            item = banco_mod._item_mesclado(atual, item)
                            unidade = item.get("unidadeOrgao") or {}
                            if classificacao_mod._local_bloqueado(
                                unidade.get("ufSigla"), unidade.get("municipioNome")
                            ):
                                continue
                            entidade = item.get("orgaoEntidade") or {}
                            texto_classificacao = classificacao_mod._texto_para_classificar(
                                item.get("objetoCompra"), item.get("informacaoComplementar")
                            )
                            detalhes = classificacao_mod.classificar(
                                texto_classificacao,
                                detalhado=True,
                                config=config,
                                orgao=" ".join(
                                    str(v or "")
                                    for v in (
                                        entidade.get("razaoSocial"),
                                        unidade.get("nomeUnidade"),
                                    )
                                ),
                            )
                            if detalhes["manter"]:
                                movimento = banco_mod.salvar_licitacao(
                                    conn,
                                    item,
                                    (detalhes["score"], detalhes["termos"], detalhes["principal"]),
                                    atual=atual,
                                    rodada_id=rodada_id,
                                    config=config,
                                    dia_coleta=hoje,
                                )
                                novas += movimento == "NOVA"
                                atualizadas += movimento == "ATUALIZADA"
                                relevantes.add(numero)
                                existentes.add(numero)
                            elif (
                                atual
                                or detalhes["motivo"] == "topografia/agrimensura sem serviço-alvo"
                            ):
                                banco_mod._salvar_descartada(
                                    conn,
                                    item,
                                    detalhes["motivo"],
                                    detalhes,
                                    atual,
                                    rodada_id,
                                    config=config,
                                    dia_coleta=hoje,
                                )
                                descartadas += 1
                                relevantes.discard(numero)
                                existentes.add(numero)
                        conn.execute(
                            """INSERT INTO paginas_coleta
                            (dia,uf,modalidade,assinatura,inicio,fim,ultima_pagina,rodada_id) VALUES (?,?,?,?,?,?,?,?)
                            ON CONFLICT(dia,uf,modalidade) DO UPDATE SET ultima_pagina=excluded.ultima_pagina,
                            assinatura=excluded.assinatura,inicio=excluded.inicio,fim=excluded.fim,rodada_id=excluded.rodada_id""",
                            (
                                dia,
                                uf,
                                modalidade,
                                assinatura,
                                dia,
                                data_final.isoformat(),
                                pagina,
                                rodada_id,
                            ),
                        )
                        conn.commit()
                        if pagina >= resultado.get("totalPaginas", 1):
                            total_esperado = resultado.get("totalRegistros")
                            if total_esperado is not None and len(numeros_combinacao) < total_esperado:
                                configuracao_mod.log.warning(
                                    "%s/modalidade %s incompleta: %s editais distintos de %s anunciados",
                                    uf, modalidade, len(numeros_combinacao), total_esperado,
                                )
                                total_falhas += 1
                                combo_ok = False
                            break
                        pagina += 1
                    if combo_ok:
                        conn.execute(
                            "INSERT OR IGNORE INTO progresso (dia,uf,modalidade,assinatura,rodada_id) VALUES (?,?,?,?,?)",
                            (dia, uf, modalidade, assinatura, rodada_id),
                        )
                        feitos.add((uf, modalidade))
                        conn.execute(
                            "DELETE FROM paginas_coleta WHERE dia=? AND uf=? AND modalidade=?",
                            (dia, uf, modalidade),
                        )
                    if interrompido:
                        break
                    if not combo_ok:
                        configuracao_mod.log.warning(
                            "%s/modalidade %s pendente para --retomar. Seguindo para a próxima combinação.",
                            uf, modalidade,
                        )
                conn.commit()
                chamadas += metricas_uf["chamadas"]
                duracao_uf = time.monotonic() - inicio_uf
                pendentes_uf = sum((uf, modalidade) not in feitos for modalidade in modalidades)
                configuracao_mod.log.info(
                    f"{uf} | {pendentes_uf} modalidades pendentes | "
                    f"escaneadas: {total_escaneados - escaneados_antes} | duração: {duracao_uf:.1f}s | "
                    f"chamadas: {metricas_uf['chamadas']} ({metricas_uf['resposta_s']:.1f}s de resposta) | "
                    f"pausa normal: {metricas_uf['pausa_s']:.1f}s | espera 429: {metricas_uf['espera_429_s']:.1f}s"
                )

            estado = (
                "completa"
                if total_falhas == 0 and not interrompido
                else "parcial" if consultas_ok or atualizadas else "falhou"
            )
            if estado == "completa" and consultas_ok:
                chamadas_detalhe, mudancas = atualizar_situacoes(
                    conn, sessao, rodada_id, hoje, data_final, prazo_execucao, delay, config
                )
                chamadas += chamadas_detalhe
                atualizadas += mudancas
            pendentes = total_combinacoes - len(feitos)
            banco_mod.registrar_execucao(
                conn,
                total_escaneados,
                len(relevantes),
                total_falhas,
                interrompido,
                estado,
                rodada_id,
                dia_coleta=hoje,
                chamadas=chamadas,
                pendentes=pendentes,
                tentativa_id=tentativa_id,
            )
            conn.execute("DELETE FROM progresso WHERE dia < ?", (dia,))
            conn.execute("DELETE FROM paginas_coleta WHERE dia < ?", (dia,))
            conn.commit()
            configuracao_mod.log.info(
                f"Coleta finalizada. {chamadas} chamadas | {total_escaneados} escaneadas | "
                f"{len(relevantes)} relevantes | {pendentes} combinações pendentes | "
                f"{total_falhas} falhas | {estado}"
            )
            configuracao_mod.log.info(
                f"Movimentos na coleta: {novas} novas | {atualizadas} atualizadas | "
                f"{descartadas} descartadas"
            )
            return {
                "estado": estado,
                "falhas": total_falhas,
                "escaneados": total_escaneados,
                "rodada_id": rodada_id,
                "tentativa_id": tentativa_id,
                "dia": hoje,
                "chamadas": chamadas,
                "pendentes": pendentes,
                "total_combinacoes": total_combinacoes,
            }
        finally:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()


def testar_conexao_pncp(config, tamanho_pagina):
    hoje = configuracao_mod.hoje_brasilia()
    fim = hoje + timedelta(days=90)
    uf, modalidade = config["ufs"][0], config["modalidades"][0]
    resposta = consultar_pagina(
        1, modalidade, uf, hoje.strftime("%Y%m%d"), fim.strftime("%Y%m%d"),
        tentativas=1, timeout=20, tamanho_pagina=tamanho_pagina,
    )
    if resposta == "FALHOU":
        return 1
    try:
        pagina = validar_pagina(resposta, 1)
    except ValueError as erro:
        configuracao_mod.log.error("O PNCP respondeu, mas o Radar não conseguiu validar a página: %s", erro)
        return 1
    encontrados = len(pagina["data"]) if pagina else 0
    configuracao_mod.log.info("Conexão com o PNCP confirmada: %s/modalidade %s retornou %s itens na primeira página "
             "(tamanho solicitado: %s).", uf, modalidade, encontrados, tamanho_pagina)
    return 0
