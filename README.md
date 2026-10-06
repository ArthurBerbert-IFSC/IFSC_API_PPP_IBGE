# IFSC_API_PPP_IBGE — PPP em sessões curtas (IBGE-PPP × RBMC)

Programa para avaliar quanto tempo de rastreio GNSS é preciso para que a coordenada obtida no
[IBGE-PPP](https://www.ibge.gov.br/geociencias/informacoes-sobre-posicionamento-geodesico/servicos-para-posicionamento-geodesico/16334-servico-online-para-pos-processamento-de-dados-gnss-ibge-ppp.html)
fique dentro da tolerância cadastral em relação à **coordenada oficial** das estações da RBMC.

Continua o estudo de França, Araújo e Lima (COBRAC 2024), que comparou soluções PPP de 24 h com as
coordenadas oficiais de dez estações da RBMC. Aqui a pergunta é a do campo: com sessões curtas,
quantas horas bastam, e o horário do rastreio importa?

## O que o programa faz

1. **Baixa** o RINEX diário (15 s) das estações da RBMC nos dias escolhidos (RINEX 2 do geoftp; se não
   houver, RINEX 3 pela API do IBGE) e o relatório de cada estação, de onde tira a coordenada oficial
   SIRGAS2000 (época 2000,4) e o código SAT.
2. **Corta** sessões de 1, 2, 3 e 4 h começando às 8 h (manhã) e às 13 h (tarde), no **horário solar local**
   (estimado pela longitude de cada estação), e mantém o arquivo de 24 h como referência do dia. Cada sessão mais longa contém a mais curta do mesmo
   período.
3. **Envia** cada sessão à API do IBGE-PPP, **uma de cada vez**, esperando a resposta e mais 5 s antes da
   próxima. As coordenadas vêm no resumo da própria resposta.
4. **Analisa**: diferenças em norte, leste e altitude para a coordenada oficial e para a solução de 24 h
   do mesmo dia; porcentagem de sessões dentro de 80 mm (NBR 17047); manhã × tarde; duas ocupações
   (manhã + tarde) × sessão contínua; incerteza informada no relatório × erro real.

## Instalação

Python 3.9 ou mais novo, com Tkinter (já vem no instalador do python.org).

```
pip install -r requirements.txt
```

No Windows, basta clicar duas vezes em **`rodar.bat`**: ele instala o que faltar e abre o programa.

## Uso

### Com janela

```
python ppp_gui.py
```

Preencha o e-mail (o IBGE-PPP exige um e-mail e manda uma mensagem por processamento), confira dias,
estações e durações, e clique em **Fazer tudo**. O programa baixa, corta e manda uma sessão de teste;
se o teste der certo, pergunta se pode enviar o restante. A janela mostra a etapa, a barra de progresso,
o contador de sessões, o tempo decorrido, o tempo restante estimado e a contagem regressiva das esperas.

O botão **Parar** interrompe no próximo passo seguro. Tudo pode ser retomado: nada já baixado,
cortado ou processado é refeito.

### Pela linha de comando

```
python ppp_sessoes.py baixar
python ppp_sessoes.py cortar
python ppp_sessoes.py testar-api --email seu@email
python ppp_sessoes.py enviar --email seu@email
python ppp_sessoes.py analisar
```

Opções: `--dias 2026-08-12 2026-08-26`, `--estacoes POAL RNNA`, `--duracoes 1 2 3 4`.

### Resultados (pasta `saida`)

| Arquivo | Conteúdo |
|---|---|
| `resultados_ppp_sessoes.xlsx` | tabelas por duração e período, manhã + tarde, por estação, e todas as sessões |
| `figura_divergencia_por_duracao.png` | divergência planimétrica e altimétrica por duração, manhã × tarde |
| `numeros_resumo.json` | os números usados no texto |
| `*.csv` | as mesmas tabelas em CSV (separador `;`, vírgula decimal) |

A pasta `dados` guarda RINEX, sessões, respostas do IBGE-PPP e o registro `execucao.log`.

## Detalhes que importam

- **Antena.** O IBGE-PPP só aplica a correção do centro de fase se o modelo vier no formato do IGS
  (modelo em 16 colunas + radome, como no cabeçalho RINEX: `TRM115000.00    NONE`). Com o nome
  abreviado a resposta traz `ANTENA: NÃO DISPONÍVEL` e a altitude sai com erro de decímetros.
  O programa confere isso em cada resposta e tenta outros formatos se preciso.
- **Firewall.** A API recusa alguns envios com a página "Request Rejected". O programa envia como no
  exemplo do manual do IBGE-PPP (RINEX compactado em Hatanaka, `.yyd`) e, se for recusado, tenta outras
  formas documentadas, guardando a que funcionou em `dados/api_variante.json`.
- **Resultado.** O link do ZIP de resultado fica atrás de uma verificação do Cloudflare e não é baixado.
  As coordenadas vêm do resumo da resposta da API, com altitude em centímetros (2 casas).
- **Órbitas.** Use dias com pelo menos 18 dias de antecedência, para o IBGE-PPP usar órbita final.
- **Estação CRUZ.** Sem dados em 2026 no IBGE; o programa segue com as demais.

## Teste

O teste roda o fluxo inteiro contra um servidor local que imita o IBGE, com dados sintéticos:

```
python tests/testar_fluxo.py
```

## Referências

- FRANÇA, R. M.; ARAÚJO, A. L.; LIMA, A. P. B. Uso de posicionamento por ponto preciso no cadastro
  territorial. In: COBRAC 2024, Florianópolis. *Anais* [...]. UFSC, 2024.
- IBGE. *Manual do usuário IBGE-PPP*: serviço on-line para pós-processamento de dados GNSS. Versão junho
  de 2023. Rio de Janeiro: IBGE, 2023.
- ABNT. *NBR 17047*: levantamento cadastral territorial para registro público — procedimento. 2022.

## Como citar

LIMA, Arthur Peixoto Berbert. **IFSC_API_PPP_IBGE**: programa para avaliar o IBGE-PPP em sessões curtas.
Florianópolis, 2026. Programa de computador. Disponível em:
https://github.com/ArthurBerbert-IFSC/IFSC_API_PPP_IBGE.

## Licença

MIT — veja `LICENSE`.
