# Radar PNCP v5

## Sobre o projeto

Sou estudante do segundo semestre de Engenharia de Software e trabalho como jovem aprendiz na Progaia. Criei este projeto para aprender, na prática, a consumir uma API e para ajudar a resolver um problema pequeno, mas recorrente no trabalho: encontrar oportunidades de licitação relacionadas aos serviços da empresa exigia fazer buscas e captações manuais.

O Radar consulta dados públicos do Portal Nacional de Contratações Públicas (PNCP), procura oportunidades ligadas principalmente a serviços ambientais e áreas técnicas relacionadas e organiza os resultados em uma planilha diária. A ideia é reduzir o trabalho de busca e facilitar a triagem. O programa não decide se uma licitação deve ser disputada e não substitui a leitura do edital.

Este projeto também é uma forma de estudo. Durante o desenvolvimento, pratiquei integração com API, tratamento de respostas JSON, paginação, classificação por palavras-chave, persistência com SQLite e geração de planilhas com Python.

## O que o programa faz

1. Consulta a API de contratações com propostas abertas do PNCP.
2. Pesquisa as 27 unidades federativas e as modalidades de contratação configuradas no script.
3. Analisa o objeto da contratação, a informação complementar disponível e o contexto do órgão ou da unidade.
4. Classifica as oportunidades por termos e pontuação, para priorizar resultados relacionados ao foco do Radar.
5. Salva e atualiza os registros em um banco SQLite, mantendo histórico de alterações relevantes.
6. Gera uma planilha Excel com oportunidades organizadas por prazo, prioridade, situação e movimento.
7. Guarda a marcação **Já captei?** feita na planilha para reaproveitá-la na próxima execução.

O período de busca vai da data da execução até os 90 dias seguintes. A consulta percorre as páginas retornadas pela API e usa pausas e novas tentativas para lidar com falhas temporárias e limites de requisição.

## Como a classificação funciona

O Radar procura termos ligados, por exemplo, a licenciamento ambiental, estudos ambientais, restauração florestal, manejo arbóreo, fauna, arqueologia e monitoramento ambiental. Cada termo tem um peso:

- **Peso 3:** termos mais específicos do foco do projeto.
- **Peso 2:** termos relevantes que podem precisar de contexto.
- **Peso 1:** termos genéricos, que sozinhos não costumam ser suficientes.

Em geral, a oportunidade precisa alcançar pontuação mínima 2 para ser mantida. O código também aplica regras para reduzir falsos positivos: termos genéricos podem depender do contexto do órgão ou da unidade; compras de bens são filtradas quando não há sinais de serviço; e topografia ou agrimensura isoladas não são suficientes para classificar uma oportunidade como relevante. Essas regras são heurísticas baseadas no texto recebido da API, não uma análise jurídica ou técnica do edital.

Os termos, pesos e critérios estão definidos no próprio `radar_pncp_v5.py`.

## O que aparece na planilha

A cada execução, o Radar cria ou atualiza um arquivo com a data do dia dentro de `planilhas/`. A pasta mantém as planilhas anteriores.

As abas geradas são:

- **Em Aberto:** oportunidades ainda dentro do prazo considerado pelo Radar.
- **DNIT:** oportunidades identificadas como relacionadas ao DNIT.
- **Novas:** oportunidades identificadas pela primeira vez no dia.
- **Atualizadas:** oportunidades com mudança relevante registrada.
- **Já captei:** oportunidades marcadas como captadas.
- **Sem interesse:** oportunidades que foram marcadas para não acompanhar.

Na coluna **Já captei?**, escolha **Não**, **Sim** ou **Sem interesse**. Salve e feche a planilha antes de executar o programa novamente. O Radar importa as marcações e as mantém no banco. Uma oportunidade marcada como **Sem interesse** pode voltar ao acompanhamento se a marcação for alterada para **Não** ou **Sim**.

A planilha inclui informações como objeto, órgão, estado, município, modalidade, valor estimado, situação, prazo restante, termos encontrados e link para abrir o edital. O prazo mínimo padrão é de sete dias; situações suspensas ou canceladas recebem tratamento especial no relatório.

## Como executar

### Requisitos

- Python 3 instalado.
- Acesso à internet para consultar o PNCP.
- As bibliotecas `requests`, `pandas` e `openpyxl`.

Instale as dependências com:

```bash
python -m pip install requests pandas openpyxl
```

O SQLite já faz parte da biblioteca padrão do Python. Não é necessário instalar um servidor de banco de dados.

### Execução normal

Abra um terminal nesta pasta e execute:

```bash
python radar_pncp_v5.py
```

O programa consulta o PNCP, atualiza o banco e salva a planilha do dia em `planilhas/`. Uma coleta completa percorre as combinações de estados e modalidades e pode levar algum tempo.

### Refazer a busca do dia

Se quiser consultar novamente todas as combinações no mesmo dia, execute:

```bash
python radar_pncp_v5.py --nova-varredura
```

Essa opção apaga apenas os checkpoints da coleta do dia atual. As oportunidades salvas e as marcações **Já captei?** são preservadas.

### Retomada após interrupção

Na execução normal, o programa registra quais combinações de estado e modalidade foram concluídas. Se a coleta for interrompida por falhas consecutivas, uma nova execução no mesmo dia continua pelas combinações que faltam. A opção `--nova-varredura` força a repetição de todas elas.

> Antes de executar novamente, salve e feche o arquivo do Excel. Se a planilha estiver aberta, o programa pode não conseguir atualizá-la.

## Banco de dados

O arquivo `licitacoes_progaia.db` é um banco SQLite usado pelo Radar. Ele fica nesta pasta e é aberto pelo próprio script. As tabelas operacionais são:

| Tabela | Finalidade |
| --- | --- |
| `licitacoes` | Registros encontrados, situação atual, classificação e marcação de captação. |
| `historico_alteracoes` | Alterações relevantes observadas nos dados de uma oportunidade. |
| `log_execucoes` | Resumo das execuções, incluindo resultados e falhas. |
| `progresso` | Checkpoints por data, estado e modalidade para retomar a coleta. |

O número de controle PNCP identifica cada oportunidade e evita duplicações. A primeira data em que o Radar encontrou um registro é preservada para distinguir oportunidades novas. O histórico permite acompanhar mudanças de situação e prazo; uma mudança isolada no campo de atualização da API não é tratada automaticamente como alteração relevante.

## Estrutura da pasta

```text
Radar PNCP v5/
├── radar_pncp_v5.py          # Script principal
├── licitacoes_progaia.db     # Banco operacional do Radar
├── README.md                 # Este guia
├── planilhas/                 # Planilhas diárias geradas
├── tests/                     # Testes do Radar e da classificação
└── HISTORICO/                 # Versões e materiais de etapas anteriores
```

O histórico foi mantido como referência da evolução do projeto. Para continuar o desenvolvimento e executar a versão atual, use o `radar_pncp_v5.py` da raiz; os scripts dentro de `HISTORICO/` são versões antigas.

## Limites conhecidos

- A classificação depende dos textos disponibilizados pelo PNCP e das regras de palavras-chave do script. Uma oportunidade pode não aparecer se a descrição não usar termos reconhecidos, ou pode exigir revisão por ter sido classificada de forma ampla.
- O Radar organiza oportunidades para facilitar a triagem; a equipe deve consultar o PNCP e ler o edital e seus anexos antes de tomar qualquer decisão.
- A execução depende da disponibilidade da API e de uma conexão com a internet.
- As marcações feitas na planilha devem ser salvas antes da próxima execução para serem importadas.

## Possíveis melhorias futuras

Como projeto de aprendizagem, há espaço para evoluir em várias direções: separar configurações do código, documentar melhor os critérios de classificação, ampliar a observabilidade da coleta, automatizar verificações e criar relatórios que ajudem a entender a qualidade dos resultados ao longo do tempo. Essas ideias não fazem parte do escopo atual do Radar.
