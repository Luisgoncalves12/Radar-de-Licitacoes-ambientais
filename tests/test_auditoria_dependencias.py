import importlib.util
from pathlib import Path

import pytest

CAMINHO = Path(__file__).resolve().parents[1] / ".github/scripts/auditar_dependencias.py"
spec = importlib.util.spec_from_file_location("auditor_dependencias", CAMINHO)
auditor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auditor)


def test_22_audita_pins_condicionais_sem_mudar_versoes_ou_hashes():
    original = "exceptiongroup==1.3.1 ; python_full_version < '3.11' \\\n    --hash=sha256:abc\n"
    transcrito, pacotes = auditor.transcrever_requisitos(original)
    assert pacotes == {"exceptiongroup": "1.3.1"}
    assert "python_full_version" not in transcrito
    assert "--hash=sha256:abc" in transcrito
    assert "exceptiongroup==1.3.1" in transcrito


@pytest.mark.parametrize(
    "entrada", ["requests>=2", "", "x==1\nx==2\n", "--index-url https://exemplo.invalid"]
)
def test_22_recusa_resolucao_implicita_ou_requisitos_ambiguos(entrada):
    with pytest.raises(ValueError):
        auditor.transcrever_requisitos(entrada)
