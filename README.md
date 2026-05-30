# Analista de Opcoes

Aplicacao local para analisar opcoes da B3 com dados da OpLab quando as credenciais estiverem configuradas. Sem credenciais, ela roda em modo demo para validar o fluxo.

## Rodar

```powershell
python app.py
```

Abra: http://localhost:8000

## Publicar no Render

Este repositorio inclui `render.yaml`. No Render, crie um novo Blueprint apontando para:

```text
https://github.com/dhonne86/Analise-de-Op-es.git
```

Depois configure as variaveis secretas no servico:

```text
OPLAB_EMAIL
OPLAB_PASSWORD
```

ou:

```text
OPLAB_ACCESS_TOKEN
```

## Configurar OpLab

Crie variaveis de ambiente antes de iniciar:

```powershell
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
