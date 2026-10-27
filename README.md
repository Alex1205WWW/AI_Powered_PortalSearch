# Imóveis — automação de pesquisa imobiliária (Milestone 1)

Painel web + bot Telegram + recolha automática de anúncios (Imovirtual, RE/MAX, alertas de email do
Idealista, sites de mediadoras), deduplicação, correspondência com os critérios de cada comprador e
alertas só para a operadora. Arquitetura e decisões: `../Milestone1/Roadmap_M1.md`.

## Executar (produção, Docker Compose)

```sh
cp .env.example .env            # preencher: POSTGRES_PASSWORD, SESSION_SECRET, ADMIN_PASSWORD_HASH, PANEL_HOST, TELEGRAM_*, chaves de IA
python -m app.cli hash-password "a password"   # ou dentro do container: docker compose run --rm web python -m app.cli hash-password "..."
docker compose up -d --build
docker compose logs -f web
```

O serviço `web` corre as migrações (`alembic upgrade head`) e os seeds (`seed-geo`, `seed-sources`)
antes de arrancar. O painel fica em `https://<PANEL_HOST>` (Caddy trata do certificado).

Processos: `db` (Postgres 16) · `web` (painel) · `worker` (recolhas, correspondência, alertas,
backups) · `bot` (Telegram) · `caddy` (TLS). O Postgres nunca é exposto fora da rede do compose.

## Desenvolvimento local

```sh
python -m venv .venv && .venv/Scripts/activate      # Windows; em Linux: source .venv/bin/activate
pip install -r requirements.txt
docker run -d --name imoveis-db -e POSTGRES_USER=imoveis -e POSTGRES_PASSWORD=imoveis -e POSTGRES_DB=imoveis -p 5432:5432 postgres:16
cp .env.example .env   # DATABASE_URL=postgresql+psycopg://imoveis:imoveis@localhost:5432/imoveis, APP_ENV=dev
```

Sem Docker (Windows sem WSL): Postgres 16 portátil, sem direitos de administrador, fora do repositório.

```sh
# uma vez: descarregar https://get.enterprisedb.com/postgresql/postgresql-16.13-1-windows-x64-binaries.zip e extrair em ../.pg16/pgsql
../.pg16/pgsql/bin/initdb -D ../.pg16/data -U imoveis -A scram-sha-256 --pwfile=<ficheiro com a password> -E UTF8 --locale=C
../.pg16/pgsql/bin/pg_ctl -D ../.pg16/data -o "-p 5432" -l ../.pg16/pg.log -w start      # ... stop para parar
../.pg16/pgsql/bin/createdb -U imoveis -h localhost imoveis
alembic upgrade head
python -m app.cli seed-geo && python -m app.cli seed-sources && python -m app.cli refresh-robots
uvicorn app.web.main:app --reload --port 8000     # http://localhost:8000
pytest -q
```

## Comandos de operação (`python -m app.cli`)

| Comando | Faz |
|---|---|
| `seed-geo` | carrega `data/geo_pt.csv` (3 distritos + 50 concelhos) |
| `seed-sources` | insere as fontes conhecidas (não altera fontes já existentes) |
| `refresh-robots` | lê `robots.txt` + `Content-Signal` de cada fonte (o worker repete semanalmente) |
| `hash-password <pw>` | gera o `ADMIN_PASSWORD_HASH` |
| `send-test [texto]` | envia uma mensagem Telegram para `TELEGRAM_CHAT_ID` |
| `seed-demo-buyers [n]` / `delete-demo-buyers` | dados de teste locais |

## Backups e restauro

O worker corre `pg_dump -Fc` todas as noites (03:30) para o volume `backups`, guardando os últimos
`BACKUP_KEEP`. Manual: `scripts/backup.sh`. Restauro: `scripts/restore.sh imoveis-AAAAMMDD-HHMMSS.dump`.

## Estrutura

```
app/config.py      variáveis de ambiente (único sítio que as lê)
app/db/            modelos SQLAlchemy (esquema) e sessão
app/geo/           taxonomia distrito/concelho/freguesia
app/domain/        regras puras: critérios, correspondência, deduplicação, eventos
app/sources/       cliente HTTP, robots, adaptadores por fonte
app/ai/            transcrição, interpretação de critérios, perfil de sites (a partir do Dia 6 / Dia 5)
app/services/      operações de negócio usadas pelo painel, bot e worker
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
