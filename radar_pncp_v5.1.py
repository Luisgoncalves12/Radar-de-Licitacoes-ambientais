"""Entrada compativel do Radar PNCP. A implementacao fica no pacote radar."""

from radar.auditoria import (
    publicar_resumo,
    resumo_coleta,
)
from radar.banco import (
    COLUNAS_NOVAS,
    _bloquear_banco,
    _bloqueios_locais,
    _buscar_licitacao,
    _estado_marcacao,
    _item_mesclado,
    _ler_ultima_coleta,
    _mesclar_dados,
    _mesclar_versoes,
    _registrar_marcacao,
    _resumir_valor,
    _rodada_do_dia,
    _salvar_descartada,
    _valor_texto,
    iniciar_banco,
    reclassificar_banco,
    registrar_execucao,
    restaurar_marcacao,
    salvar_licitacao,
    ultima_coleta_salva,
)
from radar.classificacao import (
    TERMOS_CONTEXTUAIS,
    _COMPRA_EXPLICITA,
    _CONTEXTO_INFRA,
    _CONTEXTO_PLANO_MAN,
    _MENCOES_INCIDENTAIS,
    _NOME_ADMINISTRATIVO,
    _SERVICO_CONTRATADO,
    _contem,
    _filtros_compilados,
    _local_bloqueado,
    _motivo_objeto_alheio,
    _normalizar,
    _padrao_termo,
    _preparar_padroes,
    _sem_mencoes_incidentais,
    _sem_termos_administrativos,
    _tem_familia_topografia,
    _texto_para_classificar,
    assinatura_classificacao,
    classificar,
)
from radar.cli import (
    _executar,
    main,
)
from radar.coleta import (
    _validar_limites,
    assinatura_configuracao,
    atualizar_situacoes,
    coletar,
    consultar_pagina,
    segundos_retry_after,
    testar_conexao_pncp,
    validar_pagina,
)
from radar.configuracao import (
    BASE_DIR,
    DB_PATH,
    DELAY_ENTRE_REQUISICOES,
    DIAS_MINIMOS,
    FUSO_HORARIO,
    LOCAIS_BLOQUEADOS,
    PASTA_PLANILHAS,
    PRAZO_TOTAL_PADRAO,
    TAMANHO_PAGINA,
    URL,
    URL_DETALHE,
    VARIANTES_TERMOS,
    VERSAO_REGRAS,
    _configuracao,
    agora_brasilia,
    carregar_configuracao,
    configuracao_padrao,
    hoje_brasilia,
    log,
)
from radar.dados import (
    SITUACOES_CANCELADAS,
    SITUACOES_SUSPENSAS,
    _eh_dnit,
    _identificar_edital,
    _informacoes_apresentacao,
    _situacao_cancelada,
    _situacao_suspensa,
    _timestamp_brasilia,
    limpar_para_excel,
    link_pncp,
)
from radar.planilhas import (
    PlanilhaIlegivel,
    _baseline_marcacoes,
    _escrever_planilha,
    _gravar_planilha,
    _origem_marcacoes,
    _quadro_historico_marcacoes,
    exportar_excel,
    importar_marcacoes_captacao,
    montar_dataframe,
)


if __name__ == "__main__":
    raise SystemExit(main())
