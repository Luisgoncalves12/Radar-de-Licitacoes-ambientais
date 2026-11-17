from radar import banco as banco_mod, classificacao as classificacao_mod, configuracao as configuracao_mod, planilhas as planilhas_mod



def test_16_normaliza_objeto_uma_vez_apos_preparar_configuracao(monkeypatch):
    texto = "Estudos ambientais e monitoramento de fauna"
    classificacao_mod.classificar(texto)
    original = classificacao_mod._normalizar
    chamadas = []

    def contar(valor):
        chamadas.append(valor)
        return original(valor)

    monkeypatch.setattr(classificacao_mod, "_normalizar", contar)
    assert classificacao_mod.classificar(texto)
    assert chamadas.count(texto) == 1


def test_17_reclassifica_so_com_regras_novas_e_sem_regravar_igual(coleta_memoria, monkeypatch):
    from test_coleta_resiliente import registro

    for numero in range(1, 4):
        item = registro(numero)
        banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    coleta_memoria.commit()
    sql = []
    coleta_memoria.set_trace_callback(sql.append)
    banco_mod.reclassificar_banco(coleta_memoria)
    assert not any(q.lstrip().upper().startswith("UPDATE LICITACOES") for q in sql)
    original = classificacao_mod.classificar
    chamadas = []

    def contar(*args, **kwargs):
        chamadas.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(classificacao_mod, "classificar", contar)
    banco_mod.reclassificar_banco(coleta_memoria)
    assert chamadas == []
    monkeypatch.setitem(configuracao_mod.configuracao_padrao(), "score_minimo", 100)
    banco_mod.reclassificar_banco(coleta_memoria)
    assert len(chamadas) == 3


def test_18_persistencia_nao_consulta_esquema_por_item(coleta_memoria):
    from test_coleta_resiliente import registro

    item = registro()
    classificacao = classificacao_mod.classificar(item["objetoCompra"])
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao)
    coleta_memoria.commit()
    sql = []
    coleta_memoria.set_trace_callback(sql.append)
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao)
    assert not any(q.upper().startswith("PRAGMA TABLE_INFO") for q in sql)


def test_19_exportacao_le_base_uma_vez(coleta_memoria, monkeypatch, tmp_path):
    from test_coleta_resiliente import registro

    item = registro()
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    coleta_memoria.commit()
    consultas = []
    coleta_memoria.set_trace_callback(consultas.append)
    monkeypatch.setattr(planilhas_mod, "_gravar_planilha", lambda *a, **k: None)
    planilhas_mod.exportar_excel(excel_path=tmp_path / "saida.xlsx")
    leituras = [q for q in consultas if "IDRADAR" in q.upper() and "FROM LICITACOES" in q.upper()]
    assert len(leituras) == 1
    assert "SELECT *" not in leituras[0].upper()
