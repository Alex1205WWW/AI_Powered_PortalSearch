# Imóveis — automação de pesquisa imobiliária (Milestone 1)

Painel web + bot Telegram + recolha automática de anúncios (Imovirtual, RE/MAX, alertas de email do
Idealista, sites de mediadoras), deduplicação, correspondência com os critérios de cada comprador e
alertas só para a operadora. Arquitetura e decisões: `../Milestone1/Roadmap_M1.md`.

## Executar (produção, Docker Compose)

```sh
cp .env.example .env            # preencher: POSTGRES_PASSWORD, SESSION_SECRET, ADMIN_PASSWORD_HASH, PANEL_HOST, TELEGRAM_*, IMAP_*, chaves de IA
python -m app.cli hash-password "a password"   # cole a linha impressa no .env, COM as plicas: o hash tem '$' e o compose interpolaria
docker compose up -d --build
docker compose logs -f web
```

O serviço `web` corre as migrações (`alembic upgrade head`) e os seeds (`seed-geo`, `seed-sources`)
antes de arrancar. O painel fica em `https://<PANEL_HOST>` (Caddy trata do certificado).

Processos: `db` (Postgres 16) · `web` (painel) · `worker` (recolhas, correspondência, alertas,
backups) · `bot` (Telegram) · `caddy` (TLS). O Postgres nunca é exposto fora da rede do compose.

## Instalar num VPS novo (Ubuntu 24.04, Hetzner)

Pré-requisitos: servidor com 2 vCPU / 4 GB na conta da cliente, chave SSH adicionada na criação,
nome DuckDNS (gratuito, `https://www.duckdns.org`) a apontar para o IP do servidor.

**Processador ARM (Hetzner CAX) ou Intel/AMD (CX): ambos funcionam.** O servidor em produção é um
CAX11 (Ampere, `aarch64`). Toda a stack tem binários para arm64 — as imagens `postgres:16`,
`caddy:2` e `python:3.12-slim` publicam `linux/arm64/v8`, todas as dependências têm wheels
`manylinux_aarch64` e o repositório PGDG serve arm64. **Construa sempre a imagem no próprio
servidor** (`docker compose build`): uma imagem construída num portátil Intel não arranca em ARM,
e não há registry neste desenho.

**`docker compose build` sem argumentos.** Os três serviços (`web`, `worker`, `bot`) são imagens
separadas a partir do mesmo Dockerfile. Construir só `web` deixa o worker a correr código antigo —
aconteceu em 2026-10-30 e custou meia hora a diagnosticar.

```sh
ssh root@<IP>
apt-get update && apt-get -y upgrade
apt-get -y install ufw git
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
curl -fsSL https://get.docker.com | sh                     # Docker Engine + Compose plugin
mkdir -p /srv && cd /srv
# copiar o diretório imoveis/ para /srv/imoveis (scp -r, ou git clone do repositório privado)
cd /srv/imoveis && cp .env.example .env && nano .env         # APP_ENV=prod, PANEL_HOST=<nome>.duckdns.org, passwords, token do bot, IMAP
docker compose up -d --build
docker compose logs -f web                                   # esperar "Application startup complete"
curl -sS https://<nome>.duckdns.org/healthz                  # {"ok":true}
docker compose exec worker python -c "from app.worker.jobs import backup; print(backup())"   # primeiro dump
```

Depois de uma correção de dados (como a migração `0003`, que corrige as tipologias do Imovirtual):
`docker compose exec worker python -m app.cli rematch` (silencioso, sem alertas).

### Ligar o HTTPS (nome próprio da cliente, ou DuckDNS)

O painel só fica acessível de fora depois de existir um nome DNS a apontar para o servidor. Enquanto
não existir, as portas 80/443 ficam fechadas no `ufw` de propósito: um painel com palavra-passe
servido em HTTP simples é pior do que um painel inacessível. Até lá, use um túnel SSH:

```sh
ssh -L 8000:localhost:8000 root@<IP>      # depois abra http://localhost:8000 no seu browser
```

**Opção recomendada: um subdomínio do domínio dela.** Fica profissional nas ligações que o Telegram
lhe envia, é dela para sempre e evita mudar de endereço mais tarde. Ela só precisa de criar **um
registo A** no painel de DNS de quem lhe aloja o domínio:

| Campo | Valor |
|---|---|
| Tipo | **A** (não CNAME, não "redirecionamento"/"URL forwarding") |
| Nome / Host | `imoveis` (fica `imoveis.odominio-dela.pt`) |
| Valor / Aponta para | o IP do servidor |
| TTL | o mais baixo que o painel permitir (300 s), pode subir depois |

Usar um **subdomínio** não mexe no site nem no email dela: os registos MX e o `www` ficam intactos.
Se o domínio estiver atrás da Cloudflare, ponha a nuvem **cinzenta** (DNS only) neste registo.

**Alternativa gratuita:** um nome em `duckdns.org` (ou `dynv6.net`, `no-ip.org`). Precisa de conta.

**Não serve:** o nome automático da Hetzner `static.<ip>.clients.your-server.de`, nem `sslip.io` /
`nip.io`. Resolvem bem, mas o domínio-pai não está na Public Suffix List, por isso o Let's Encrypt
conta todos os clientes no mesmo limite e o certificado nunca é emitido (verificado a 2026-10-30:
zero certificados em todo o Certificate Transparency para `*.clients.your-server.de`).

Quando o nome existir, no servidor:

```sh
cd /srv/imoveis
./scripts/enable_tls.sh imoveis.odominio-dela.pt andreia@odominio-dela.pt
```

O script confirma o DNS antes de mexer em nada, escreve `PANEL_HOST` / `PANEL_BASE_URL` no `.env`,
abre as portas 80 e 443, arranca o Caddy e espera pelo certificado. Se falhar, diz porquê. A causa
mais comum não é o ufw: é a **Cloud Firewall da Hetzner**, que se configura na consola web deles e
é independente do ufw.

Verificação final:

```sh
curl -sS https://imoveis.odominio-dela.pt/healthz     # {"ok":true}
```

O certificado renova-se sozinho. Para mudar de nome mais tarde, basta correr o script outra vez.

### Primeiro arranque: aquecer os hashes antes do backfill

O backfill da RE/MAX compara fotografias com as outras fontes. Cada fotografia é descarregada **uma
vez** e o hash fica em cache para sempre, mas se a cache estiver fria o backfill paga esse custo
enquanto rastreia (medido a 2026-10-30: 25 anúncios → 316 descarregamentos, 292 deles de fotografias
do Imovirtual). Aqueça primeiro:

```sh
docker compose exec -d worker sh -c "python -m app.cli warm-hashes 8000 >/tmp/warm.log 2>&1"
docker compose exec worker python -m app.cli warm-hashes 1   # mostra quantas faltam
```

O worker também aquece sozinho, 400 de cada vez, de 15 em 15 minutos. Com a cache quente, o
backfill completo são cerca de 7 h: ~4,3 h de páginas (1,5 s cada, 10 298 anúncios) e o resto de
fotografias. Corre sozinho de 20 em 20 minutos e retoma onde ficou (`crawl_runs.cursor`), por isso
não precisa de ficar a ver. Termina quando o run de `kind='backfill'` fica em `status='ok'`;
`partial` significa que foi uma fatia deliberada (`--limit`) e que ainda falta catálogo.

### Notas de operação

```sh
docker compose exec worker sh -c "python -m app.cli backfill remax 200"   # fatia medida
docker compose exec worker sh -c "python -m app.cli poll-email --rescan --silent"  # reler a caixa após corrigir o parser
docker compose exec worker python -m app.cli matched-refresh              # detalhes dos imóveis correspondentes
docker compose exec worker python -m app.cli export /tmp/imoveis.xlsx     # folha de cálculo
```

Passe sempre as opções com `sh -c "..."`: o `docker compose exec` consome os `--flags` que vêm
soltos e o comando corre sem elas, em silêncio.

## Desenvolvimento local

```sh
python -m venv .venv && .venv/Scripts/activate      # Windows; em Linux: source .venv/bin/activate
pip install -r requirements.txt
docker run -d --name imoveis-db -e POSTGRES_USER=imoveis -e POSTGRES_PASSWORD=imoveis -e POSTGRES_DB=imoveis -p 5432:5432 postgres:16
cp .env.example .env   # DATABASE_URL=postgresql+psycopg://imoveis:imoveis@127.0.0.1:5432/imoveis, APP_ENV=dev
```

Sem Docker (Windows sem WSL): Postgres 16 portátil, sem direitos de administrador, fora do repositório.

```sh
# uma vez: descarregar https://get.enterprisedb.com/postgresql/postgresql-16.13-1-windows-x64-binaries.zip e extrair em ../.pg16/pgsql
../.pg16/pgsql/bin/initdb -D ../.pg16/data -U imoveis -A scram-sha-256 --pwfile=<ficheiro com a password> -E UTF8 --locale=C
../.pg16/pgsql/bin/pg_ctl -D ../.pg16/data -o "-p 5432" -l ../.pg16/pg.log -w start      # ... stop para parar
../.pg16/pgsql/bin/createdb -U imoveis -h 127.0.0.1 imoveis
alembic upgrade head
python -m app.cli seed-geo && python -m app.cli seed-sources && python -m app.cli refresh-robots
uvicorn app.web.main:app --reload --port 8000     # http://localhost:8000
pytest -q
```

No dia a dia basta `.\scripts\dev.ps1` a partir de `imoveis/`: arranca o Postgres portátil se estiver parado e depois o painel. Arranque-o sempre a partir do seu próprio terminal; um Postgres lançado por uma shell de ferramenta morre quando essa sessão termina. Com a base de dados parada o painel responde 503 com uma mensagem clara, e volta a ligar-se sozinho quando ela regressar.

## Comandos de operação (`python -m app.cli`)

| Comando | Faz |
|---|---|
| `seed-geo` | carrega `data/geo_pt.csv` (3 distritos + 50 concelhos) |
| `seed-sources` | insere as fontes conhecidas (não altera fontes já existentes) |
| `refresh-robots` | lê `robots.txt` + `Content-Signal` de cada fonte (o worker repete semanalmente) |
| `hash-password <pw>` | gera o `ADMIN_PASSWORD_HASH` |
| `send-test [texto]` | envia uma mensagem Telegram para `TELEGRAM_CHAT_ID` |
| `run-initial <criteria_id>` / `run-queued` | busca inicial de uma pesquisa (só painel, sem alertas) |
| `sweep <fonte>` | uma passagem incremental + correspondência + alertas (o worker faz isto por horário) |
| `poll-email` | lê os novos emails de alerta do Idealista (o worker faz isto a cada 3 min) |
| `rematch` | reavalia todas as pesquisas ativas contra os imóveis guardados, sem alertas |
| `split <listing_id>` | deduplicação: este anúncio não é o mesmo imóvel que os outros do grupo |
| `flush-notifications` / `rewind-watermarks <fonte> <horas>` | reenviar pendentes / olhar mais para trás na próxima passagem |
| `seed-demo-buyers [n]` / `delete-demo-buyers` | dados de teste locais |

## Como os dados fluem

```
adaptador (fonte) -> CanonicalListing -> services.runs.ingest()
   = services.listings.persist()      (listings, imagens, histórico de preço, mediadoras)
   + services.properties.assign()     (deduplicação -> properties)
   -> services.matches (vista agregada do imóvel x cada pesquisa ativa)
   -> domain.events (novo / baixa de preço / dado confirmado) -> services.notifications (Telegram)
```

Deduplicação (`app/domain/dedup.py`): dois anúncios são o mesmo imóvel quando, dentro do mesmo concelho,
tipo, tipologia, área ±5 % e preço ±3 %, têm a mesma (mediadora, referência) **ou** as fotografias coincidem
(pHash, só entre fontes diferentes, 2 pares quando ambos têm ≥ 2 fotos, sem andar contraditório, nunca para
empreendimentos, e só quando exatamente um imóvel candidato coincide). Em caso de dúvida ficam separados:
uma duplicação visível custa um olhar; uma fusão errada esconde uma oportunidade.

Fontes e IA: o Imovirtual declara `Content-Signal: ai-input=no`, por isso o seu conteúdo nunca é enviado a um
modelo (`app/ai/client.py` recusa). A IA só interpreta as notas de voz da operadora e perfila sites de mediadoras.

## Backups e restauro

O worker corre `pg_dump -Fc` todas as noites (03:30) para o volume `backups`, guardando os últimos
`BACKUP_KEEP`. Manual: `scripts/backup.sh`. Restauro: `scripts/restore.sh imoveis-AAAAMMDD-HHMMSS.dump`.

## Estrutura

```
app/config.py      variáveis de ambiente (único sítio que as lê)
app/db/            modelos SQLAlchemy (esquema) e sessão
app/geo/           taxonomia distrito/concelho/freguesia
app/domain/        regras puras: critérios, correspondência, deduplicação, eventos
app/sources/       cliente HTTP, robots, adaptadores por fonte (imovirtual, idealista_email, ...)
app/ai/            guarda de conformidade e ledger; transcrição e interpretação a partir do Dia 6
app/services/      operações de negócio usadas pelo painel, bot e worker (runs = pipeline de ingestão)
app/notify/        envio Telegram
app/web/           painel (FastAPI + Jinja2 + HTMX)
app/worker/        agendador (APScheduler) e jobs
app/bot/           bot Telegram (polling)
alembic/           migrações
tests/             testes unitários com fixtures guardadas
```

## Adicionar uma fonte

1. Criar `app/sources/<chave>.py` que implementa `SourceAdapter` (`app/sources/base.py`) e regista-se com `@register("<chave>")`.
2. Acrescentar a entrada em `SOURCE_SEED` (`app/sources/registry.py`) ou criar a fonte no painel.
3. Guardar uma página real em `tests/fixtures/` e escrever o teste do normalizador.
4. Tudo passa por `runs.ingest()`: deduplicação, correspondência e alertas vêm de graça.

## Runbook — operar e recuperar

### Atualizar o código no servidor

Construir **sempre no servidor** (não há registry e uma imagem amd64 não arranca na ARM), e recriar o `web`
primeiro porque é ele que corre as migrações:

```sh
# no portátil
tar -czf /tmp/imoveis.tgz --exclude=.venv --exclude=.env --exclude=__pycache__     --exclude=.pytest_cache --exclude=.git --exclude=.pg16 .
scp /tmp/imoveis.tgz root@<ip>:/tmp/

# no servidor
cd /srv/imoveis
tar -xzf /tmp/imoveis.tgz                 # o .env fica intacto: não está no arquivo
docker compose build                      # sem argumento: web, worker e bot saem do mesmo Dockerfile
docker compose up -d --force-recreate web
docker compose logs --tail=20 web | grep -i alembic     # confirmar a migração
docker compose up -d --force-recreate worker bot
```

### Cópias de segurança

O `worker` corre `pg_dump -Fc` todas as noites às 03:30 e guarda as 14 mais recentes no volume `/backups`.

```sh
./scripts/backup.sh                                     # cópia manual agora
docker compose exec worker ls -la /backups/              # ver o que existe
```

### Recuperar (e ensaiar a recuperação)

**Ensaio — não toca nos dados reais.** Restaura para uma base de dados temporária, compara as contagens e
apaga-a. Faça isto de vez em quando: um dump que nunca foi restaurado não é uma cópia de segurança.

```sh
./scripts/restore.sh imoveis-20261030-194024.dump --rehearse
```

**Recuperação a sério** (substitui a base de dados viva; pede confirmação):

```sh
./scripts/restore.sh imoveis-20261030-194024.dump
```

> O `restore.sh` **não** faz `. ./.env` — `HTTP_USER_AGENT` tem parênteses e não é shell válido, por isso
> com `set -e` o restauro abortava antes de começar. As credenciais são lidas do ambiente do contentor.
> (Defeito encontrado pelo ensaio de recuperação, 2026-10-31.)

### Acrescentar uma fonte

**Mediadora** (sem código): painel → **Mediadoras** → *Acrescentar uma mediadora à mão*, ou
`python -m app.cli agency-add https://www.mediadora.pt "Nome"`. É analisada na passagem seguinte e fica com
um nível de confiança (`full` / `partial` / `none`, com a razão quando é `none`).

**Portal novo** (com código): escrever um adaptador em `app/sources/`, registá-lo em
`app/sources/registry.py`, acrescentar a linha em `SOURCE_SEED` e correr `python -m app.cli seed-sources`.
O contrato está em `app/sources/base.py`; o `remax.py` é o exemplo de catálogo e o `imovirtual.py` o de
consulta por zona.

### Limite de gastos de IA

Painel → **Configurações**. Afeta **apenas** a transcrição de voz e a interpretação de critérios; a recolha,
a comparação e os alertas continuam a funcionar com o limite atingido. `python -m app.cli ai-spend` mostra o
gasto do mês por operação.

### Testes

```sh
.venv/Scripts/python -m pytest -q          # unitários, sem rede e sem base de dados
.venv/Scripts/python -m scripts.smoke      # Dias 1-4 contra a base de dados real
.venv/Scripts/python -m scripts.smoke_day6 # intake, rascunhos, horário, /faltou
.venv/Scripts/python -m scripts.smoke_day7 # teto de IA, pausa, contagem do teste
```
