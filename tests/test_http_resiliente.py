from radar import banco as banco_mod, cli as cli_mod, coleta as coleta_mod

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest



@pytest.fixture
def relogio(monkeypatch):
    class Relogio:
        segundos = 0.0
        pausas = []

        def monotonic(self):
            return self.segundos

        def sleep(self, segundos):
            self.pausas.append(segundos)
            self.segundos += segundos

    relogio = Relogio()
    monkeypatch.setattr(coleta_mod.time, "monotonic", relogio.monotonic)
    monkeypatch.setattr(coleta_mod.time, "sleep", relogio.sleep)
    return relogio


@pytest.mark.parametrize("cabecalho", ["Wed, 04 Nov 2026 17:00:00 GMT", "-4", "inválido"])
def test_14_retry_after_nao_interrompe_com_formato_valido_ou_invalido(
    monkeypatch, relogio, cabecalho, sessao_http
):
    resposta = Mock(status_code=429, headers={"Retry-After": cabecalho})
    monkeypatch.setattr(sessao_http, "get", Mock(return_value=resposta))
    assert (
        coleta_mod.consultar_pagina(1, 4, "DF", "20261104", "20270202", tentativas=1, sessao=sessao_http)
        == "FALHOU"
    )
    assert all(0 <= pausa <= 60 for pausa in relogio.pausas)


def test_14_retry_after_respeita_teto_configuravel(monkeypatch, relogio, sessao_http):
    resposta = Mock(status_code=429, headers={"Retry-After": "999999999"})
    monkeypatch.setattr(sessao_http, "get", Mock(return_value=resposta))
    assert (
        coleta_mod.consultar_pagina(
            1, 4, "DF", "20261104", "20270202", limite_espera=3, sessao=sessao_http
        )
        == "FALHOU"
    )
    assert sum(relogio.pausas) <= 3


def test_14_prazo_total_limita_novas_tentativas(monkeypatch, relogio, sessao_http):
    def falhar(*args, **kwargs):
        relogio.segundos += kwargs["timeout"]
        raise coleta_mod.requests.exceptions.Timeout()

    get = Mock(side_effect=falhar)
    monkeypatch.setattr(sessao_http, "get", get)
    assert (
        coleta_mod.consultar_pagina(
            1,
            4,
            "DF",
            "20261104",
            "20270202",
            tentativas=100,
            prazo_consulta=5,
            prazo_execucao=4,
            sessao=sessao_http,
        )
        == "FALHOU"
    )
    assert relogio.segundos <= 4
    assert get.call_count == 1


def test_14_converte_data_http_para_segundos():
    agora = datetime(2026, 11, 4, 16, 59, 50, tzinfo=timezone.utc)
    assert coleta_mod.segundos_retry_after("Wed, 04 Nov 2026 17:00:00 GMT", agora) == 10


def test_14_prazo_execucao_zero_nao_faz_requisicao(relogio, sessao_http):
    assert (
        coleta_mod.consultar_pagina(
            1, 4, "DF", "20261104", "20270202", prazo_execucao=0, sessao=sessao_http
        )
        == "FALHOU"
    )
    sessao_http.get.assert_not_called()


def test_falha_de_proxy_deixa_consulta_pendente_sem_repetir(relogio, sessao_http, caplog):
    sessao_http.get.side_effect = coleta_mod.requests.exceptions.ProxyError("conexão recusada")
    assert coleta_mod.consultar_pagina(1, 4, "AC", "20261104", "20270202", sessao=sessao_http) == "FALHOU"
    sessao_http.get.assert_called_once()
    assert not relogio.pausas
    assert "Falha de proxy em AC/modalidade 4/página 1" in caplog.text


def test_http_400_nao_repete_consulta(relogio, sessao_http, caplog):
    sessao_http.get.return_value = Mock(status_code=400)
    assert coleta_mod.consultar_pagina(1, 4, "AC", "20261104", "20270202", sessao=sessao_http) == "FALHOU"
    sessao_http.get.assert_called_once()
    assert not relogio.pausas
    assert "HTTP 400" in caplog.text


@pytest.mark.parametrize(
    ("resposta", "codigo"),
    [(None, 0), ("FALHOU", 1), ({"mensagem": "resposta inválida"}, 1)],
)
def test_cli_testa_uma_consulta_sem_abrir_banco(monkeypatch, resposta, codigo):
    monkeypatch.setattr("sys.argv", ["radar", "--testar-conexao"])
    consulta = Mock(return_value=resposta)
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    monkeypatch.setattr(banco_mod, "_bloquear_banco", Mock(side_effect=AssertionError("Banco acessado")))

    assert cli_mod.main() == codigo
    consulta.assert_called_once()
    assert consulta.call_args.args[:3] == (1, 4, "AC")
    assert consulta.call_args.kwargs["tentativas"] == 1
    assert consulta.call_args.kwargs["tamanho_pagina"] == 50


@pytest.fixture
def sessao_http():
    return Mock()
