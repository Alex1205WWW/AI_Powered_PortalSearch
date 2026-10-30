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
