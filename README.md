# Analista de Opcoes

Aplicacao local para analisar opcoes da B3 usando, por padrao, as paginas publicas gratuitas da OpLab com cotacoes atrasadas. O app salva um snapshot diario por ativo para evitar consultas repetidas.

## Rodar

```powershell
python app.py
```

Abra: http://localhost:8000

## Publicar no Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/dhonne86/Analise-de-Op-es)

Este repositorio inclui `render.yaml`. No Render, crie um novo Blueprint apontando para:

```text
https://github.com/dhonne86/Analise-de-Op-es.git
```

O app funciona sem secrets usando o snapshot gratuito diario. Configure secrets apenas se quiser ativar a API PRO.

## Fonte gratuita diaria

Por padrao, o app consulta:

```text
https://opcoes.oplab.com.br/mercado/acoes/opcoes/{ATIVO}
```

Os dados sao salvos em cache local por dia em `data/oplab-free`. No Render, o disco gratuito pode ser recriado entre deploys; ainda assim a aplicacao volta a consultar a pagina publica quando o cache nao existir.

## Modo pregao

Em dias uteis, durante a janela configurada em `MARKET_OPEN` e `MARKET_CLOSE`, o snapshot gratuito expira a cada 15 minutos e a tela agenda atualizacao automatica. Fora do pregao, o cache fica mais longo para evitar consultas desnecessarias.

Variaveis opcionais:

```text
MARKET_OPEN=10:00
MARKET_CLOSE=18:00
FREE_CACHE_OPEN_SECONDS=900
FREE_CACHE_CLOSED_SECONDS=86400
```

Importante: a fonte gratuita publica da OpLab possui atraso. Para tick a tick real-time, e necessario contratar uma fonte autorizada/API paga.

## Configurar API PRO da OpLab

Somente se voce tiver plano com API, ligue a API paga explicitamente:

```powershell
$env:OPLAB_USE_PAID_API="1"
$env:OPLAB_EMAIL="seu-email"
$env:OPLAB_PASSWORD="sua-senha"
python app.py
```

Ou use token:

```powershell
$env:OPLAB_ACCESS_TOKEN="seu-token"
python app.py
```

Se sua conta usar rotas diferentes, ajuste:

```powershell
$env:OPLAB_UNDERLYING_PATH="/v3/market/stocks/{symbol}"
$env:OPLAB_OPTIONS_PATH="/v3/market/options/{symbol}"
```

Observacao: a API da OpLab e de uso pessoal, depende de plano com API e tem limites de chamada. Nao exponha credenciais no navegador.
