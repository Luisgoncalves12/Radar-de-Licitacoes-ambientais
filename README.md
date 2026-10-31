# Radar de Licitações Ambientais

Um projeto pessoal para encontrar e organizar oportunidades de licitação relacionadas a serviços ambientais e de engenharia no Portal Nacional de Contratações Públicas (PNCP).

## Como esse projeto começou

Tenho 18 anos, sou jovem aprendiz na Progaia e estou no segundo semestre de Engenharia de Software. Queria aprender mais sobre APIs e, ao mesmo tempo, percebi que a busca por editais na empresa tomava bastante tempo.

Fui pesquisar se havia uma API que pudesse ajudar e encontrei a do PNCP. Comecei a testar aos poucos e priorizei oportunidades do DNIT, órgão com o qual a Progaia já tem relacionamento. Com a ajuda dos analistas de licitações, defini um critério importante: o Radar descarta oportunidades com menos de sete dias para o fim do prazo, porque esse tempo costuma ser curto para analisar o edital e preparar uma proposta.

O projeto nasceu para facilitar a busca e a triagem. Ele não decide se uma oportunidade serve para a empresa: essa avaliação continua dependendo da leitura do edital e da equipe.

## O que o Radar faz

- Consulta oportunidades com propostas abertas na API pública do PNCP, em todas as UFs e modalidades configuradas.
- Compara os textos das oportunidades com palavras-chave relacionadas ao foco ambiental e de engenharia.
- Salva os dados e as mudanças observadas em um banco SQLite.
- Gera uma planilha Excel diária, organizada em abas como **Em Aberto**, **DNIT**, **Novas**, **Atualizadas**, **Já captei** e **Sem interesse**.
- Mantém as marcações feitas na coluna **Já captei?** entre as execuções.

O Radar olha até 90 dias à frente. Na planilha, as oportunidades aparecem com informações como prazo, órgão, município, valor estimado, termos encontrados e link para o edital.

## Como o código funciona, do início ao fim

O arquivo `radar_pncp_v5.1.py` executa o processo em duas etapas principais: primeiro coleta e atualiza os dados; depois monta a planilha. Dá para pensar nele como uma linha de triagem: ele pergunta ao PNCP, confere cada resultado, guarda o que encontrou e organiza uma lista para a equipe revisar.

1. **O programa prepara a busca.** Ao iniciar, o Python lê `configuracao.json`, que contém os estados (UFs), modalidades, palavras-chave, pesos e outras regras da triagem. A data final da consulta é calculada para até 90 dias à frente. O arquivo `.db` é o banco SQLite: um pequeno banco de dados local, armazenado em um arquivo no computador.

2. **O Radar consulta o PNCP.** Para cada combinação de estado e modalidade, `consultar_pagina` envia uma requisição à API pública. API é uma forma padronizada de um programa pedir informações a outro. A resposta vem em JSON, um formato organizado de dados que o Python consegue ler. Como os resultados são divididos em páginas, o Radar consulta as páginas seguintes até terminar aquela combinação. Se houver limite de acesso ou falha temporária, ele espera e tenta novamente.

3. **Cada oportunidade passa pela triagem automática.** O código junta o objeto da contratação com a informação complementar, quando disponível, e compara o texto com os termos configurados. Antes de comparar, normaliza maiúsculas e acentos (por exemplo, “ambiental” e “AMBIENTAL” são tratados da mesma forma). Cada termo encontrado pode somar pontos conforme seu peso. Algumas expressões só contam quando aparecem junto de um contexto adequado; há também regras para reduzir resultados de compras de bens e de topografia sem serviço relacionado. A pontuação mínima inicial é 2 e pode ser alterada em `configuracao.json`. Score é apenas uma pontuação de relevância para organizar a busca: não é uma probabilidade nem uma decisão sobre participar da licitação.

4. **Os resultados e as mudanças ficam registrados.** O número de controle do PNCP identifica cada contratação. O banco usa esse identificador para reconhecer quando já viu uma oportunidade e atualizar seus dados em vez de criar uma cópia. Também guarda a primeira e a última coleta, o score, os termos encontrados, dados recebidos da API e alterações relevantes. Assim, o Radar consegue distinguir oportunidades novas de oportunidades atualizadas.

5. **A coleta pode continuar após uma interrupção.** Quando termina uma combinação de estado e modalidade sem erro, o Radar grava um checkpoint — uma marca que indica até onde a coleta chegou. Se a execução parar, a próxima execução normal pula as combinações já concluídas naquele dia e retoma as restantes. A opção `--nova-varredura` apaga os checkpoints do dia para consultar tudo novamente.

6. **O banco vira uma planilha de trabalho.** Depois da coleta, `montar_dataframe` lê os dados do SQLite com pandas, uma biblioteca do Python para organizar tabelas. Nessa etapa, o Radar calcula os dias restantes, identifica novas e atualizadas, prepara os links e aplica os filtros de apresentação, como o prazo mínimo de sete dias. O Excel é então gravado com abas para a lista em aberto, DNIT, novidades, atualizações e acompanhamento das marcações.

7. **As marcações da equipe voltam para o banco.** Antes de gerar a planilha, o programa lê a planilha diária anterior e importa os valores da coluna **Já captei?**. Por isso, a equipe pode marcar **Sim**, **Não** ou **Sem interesse** sem perder essas escolhas na próxima execução. Se a planilha estiver aberta no Excel, o arquivo pode ficar bloqueado; nesse caso, feche-o antes de rodar o programa.

Em uma linha, o fluxo é: **configurações → API do PNCP → classificação → banco SQLite → organização dos dados → planilha Excel → importação das marcações na próxima execução**. A automação reduz o trabalho de localizar e ordenar oportunidades, mas a leitura do edital e a decisão final continuam sendo feitas pela equipe.

### Cores da planilha

As cores ajudam a bater o olho e entender o que aconteceu com cada oportunidade:

- **Verde:** oportunidade nova, identificada na coleta do dia.
- **Amarelo:** oportunidade com atualização relevante.
- **Cinza:** oportunidade que não apareceu na coleta mais recente.
- **Azul-claro:** oportunidade marcada como **Sim** na coluna **Já captei?**.
- **Vermelho/rosa:** oportunidade suspensa ou cancelada. Esse destaque tem prioridade sobre as outras cores.
- **Azul-escuro na coluna Prioridade:** oportunidade relacionada ao DNIT.

## Como funciona a pontuação

As palavras-chave têm pesos diferentes: termos mais específicos valem mais pontos; termos mais amplos valem menos e precisam de contexto. O Radar também considera informações complementares da API e dados do órgão ou da unidade para avaliar alguns termos.

No começo, percebi que resultados com pontuação 1 traziam falsos positivos demais. Por padrão, a pontuação mínima é 2. Também há filtros para reduzir resultados que parecem compras de bens ou que mencionam topografia sem outros sinais de um serviço relacionado. São critérios automáticos de triagem, então ainda podem ocorrer falsos positivos ou oportunidades relevantes que não sejam encontradas.

## Como editar a configuração

O arquivo `configuracao.json`, na mesma pasta de `radar_pncp_v5.1.py`, reúne as opções de busca e triagem. Abra-o em um editor de texto e altere os valores desejados:

| Chave | O que controla |
| --- | --- |
| `termos` | Palavras ou expressões procuradas e seus pesos: 3 (específico), 2 (relevante) ou 1 (genérico). |
| `palavras_curtas` | Siglas curtas consideradas na busca de termos. |
| `exclusoes` e `resgate` | Padrões para identificar compras de bens e sinais de contratação de serviço ou obra. |
| `score_minimo` | Pontuação mínima para manter uma oportunidade. O valor inicial é 2. |
| `topografia_familia` | Expressões de topografia que não somam pontos sozinhas. |
| `modalidades`, `nomes_modalidades` e `ufs` | Modalidades consultadas, seus nomes exibidos e estados pesquisados. |
| `status_captacao` e `rotulos_captacao` | Valores e rótulos das marcações **Já captei?**. |
| `sugestoes_novos_termos` | Sugestões guardadas para avaliação; não entram na classificação automaticamente. |

Para adicionar um termo, inclua uma entrada em `termos`, por exemplo:

```json
"novo serviço ambiental": 2
```

Separe as entradas com vírgulas, exceto a última de cada lista ou objeto. Use aspas duplas e salve o arquivo em UTF-8. Nos padrões de `exclusoes` e `resgate`, a barra invertida precisa aparecer duplicada no JSON, como em `"\\b"`. Para ativar uma sugestão, acrescente-a também em `termos` com um peso.

O Radar lê o JSON ao iniciar. As mudanças passam a valer na próxima execução; um JSON com erro de sintaxe interrompe o programa antes da coleta.

## A planilha

As imagens abaixo mostram algumas das abas geradas pelo Radar:

<p align="center">
  <img src="assets/readme/em-aberto.png" alt="Aba Em Aberto com oportunidades e prazo para envio de propostas" width="700">
  <br><em>Em Aberto: oportunidades ordenadas para facilitar a triagem.</em>
</p>

<p align="center">
  <img src="assets/readme/novas.png" alt="Aba Novas com oportunidades identificadas na coleta" width="700">
  <br><em>Novas: oportunidades identificadas pela primeira vez.</em>
</p>

<p align="center">
  <img src="assets/readme/atualizadas.png" alt="Aba Atualizadas com oportunidades que tiveram mudanças" width="700">
  <br><em>Atualizadas: oportunidades com mudanças relevantes.</em>
</p>

<p align="center">
  <img src="assets/readme/ja-captei.png" alt="Aba Já captei com oportunidades marcadas como captadas" width="700">
  <br><em>Já captei: acompanhamento das oportunidades que a equipe decidiu captar.</em>
</p>

<p align="center">
  <img src="assets/readme/sem-interesse.png" alt="Aba Sem interesse com oportunidades que não serão acompanhadas" width="700">
  <br><em>Sem interesse: oportunidades que não precisam continuar na triagem.</em>
</p>

## Um pouco da evolução

Guardei as etapas anteriores na pasta `HISTORICO/`. A ideia era experimentar, observar os resultados e ajustar o Radar com o uso:

- **Primeiras versões:** comecei a consultar o PNCP e a testar a classificação por palavras-chave. Logo vieram a pontuação por relevância, o registro de todas as palavras encontradas e a identificação de oportunidades novas.
- **Versões seguintes:** fui organizando melhor as planilhas, o histórico das oportunidades e a retomada de coletas interrompidas. Também refinei o uso do contexto para reduzir resultados que não tinham relação com o foco do Radar.
- **v4:** consolidou regras de classificação, a organização das abas e o prazo mínimo de sete dias.
- **v5:** passou a considerar também a informação complementar da API, ampliou os termos ambientais e aumentou a pontuação mínima de 1 para 2 para reduzir falsos positivos. A opção `--nova-varredura` permite refazer a coleta do dia.
- **v5.1 (atual):** moveu os termos, pesos, exclusões, modalidades, UFs e outras opções da triagem para `configuracao.json`. Assim, essas regras podem ser editadas sem alterar o código Python.

Para usar a versão atual, execute o arquivo `radar_pncp_v5.1.py` na raiz do projeto. Os scripts dentro de `HISTORICO/` são versões anteriores.

## Como executar

Você vai precisar de Python 3 e acesso à internet. Instale as dependências:

```bash
python -m pip install requests pandas openpyxl
```

Para iniciar uma coleta:

```bash
python radar_pncp_v5.1.py
```

Para refazer todas as combinações de estados e modalidades no mesmo dia:

```bash
python radar_pncp_v5.1.py --nova-varredura
```

Na execução normal, se uma coleta for interrompida, o Radar registra o progresso e tenta continuar de onde parou na próxima execução. Antes de rodar o programa de novo, salve e feche a planilha do Excel para que as marcações possam ser importadas.

## Arquivos principais

- `radar_pncp_v5.1.py`: código da versão atual (v5.1).
- `configuracao.json`: parâmetros editáveis da busca e classificação.
- `assets/readme/`: imagens de exemplo das planilhas.
- `HISTORICO/`: versões anteriores e materiais de desenvolvimento.

O banco SQLite e as planilhas diárias são criados localmente durante o uso. As imagens deste README servem apenas para mostrar como o resultado fica.

## Tecnologias e aprendizados

Python, API REST, JSON, paginação, tratamento de falhas, SQLite, pandas e Excel. Foi também um exercício prático de transformar uma necessidade do dia a dia em uma ferramenta que pode ser testada e melhorada aos poucos.

## Limitações

O resultado depende das informações publicadas no PNCP e das palavras-chave configuradas. Por isso, uma oportunidade pode passar despercebida ou aparecer mesmo sem ser adequada. Consulte sempre o PNCP, o edital e seus anexos antes de tomar qualquer decisão.
