"""Operator commands. Run: python -m app.cli <command> [args]

  seed-geo                 load data/geo_pt.csv (3 districts + 50 concelhos)
  seed-sources             insert the known sources (never overwrites operator changes)
  refresh-robots           fetch robots.txt + Content-Signal for every http source
  hash-password <pw>       print the bcrypt hash for ADMIN_PASSWORD_HASH
  send-test [text]         send a Telegram message to TELEGRAM_CHAT_ID
  seed-demo-buyers [n]     create n demo buyers with one criteria + 3 zones each (local testing only)
  delete-demo-buyers       remove buyers whose label starts with "Demo "
  run-initial <id>         targeted first pass for one criteria (panel only, no alerts)
  run-queued               execute queued initial runs now (the worker does this every 30 s)
  sweep <source_key>       one incremental sweep + matching + alerts (the worker does this on schedule)
  flush-notifications      send unsent alerts
  rewind-watermarks <source_key> <hours>   look further back on the next sweep
  rematch                  silently re-evaluate every active criteria against stored properties (no alerts)
  poll-email               read new Idealista alert emails now (the worker does this every 3 min)
  split <listing_id>       dedup override: this listing is not the same property as its siblings

  agency-directory [pages] crawl the Imovirtual agency directory for her 50 concelhos (142 pages, ~5 min)
  agency-websites [n]      resolve agency websites from their Imovirtual profile pages
  agency-profile [n]       profile unprofiled agency sites (platform, pattern, measured confidence)
  agency-watch [n]         read the agency sites that are due; first read per site is silent
  agency-add <url> [name]  add one agency site by hand (her list, or anything she pastes later)
  agency-summary           how many agencies found / with a site / readable, and by confidence
  agency-evidence          listings an agency site had that no portal did (the Day 5 proof)
"""
from __future__ import annotations

import random
import sys

from sqlalchemy import select

from app.config import settings
from app.db.session import session_scope
from app.logging_setup import setup_logging


def cmd_seed_geo(_: list[str]) -> None:
    from app.geo.seed import seed_geo

    with session_scope() as session:
        print(f"geo rows merged: {seed_geo(session)}")


def cmd_seed_sources(_: list[str]) -> None:
    from app.services import sources

    with session_scope() as session:
        print(f"sources added: {sources.seed(session)}")


def cmd_refresh_robots(_: list[str]) -> None:
    from app.services import sources

    with session_scope() as session:
        for key, status, allowed in sources.refresh_robots(session):
            print(f"{key:16s} robots HTTP {status}  allowed={allowed}")


def cmd_hash_password(args: list[str]) -> None:
    from app.web.auth import hash_password

    if not args:
        sys.exit("usage: hash-password <password>")
    # Single quotes matter: docker compose interpolates '$' in .env files and would truncate the hash.
    print(f"ADMIN_PASSWORD_HASH='{hash_password(args[0])}'")


def cmd_send_test(args: list[str]) -> None:
    from app.notify import telegram

    text = " ".join(args) or "Teste do painel Imóveis: ligação ao Telegram a funcionar."
    message_id = telegram.send_message(text)
    print(f"sent message_id={message_id} to chat {settings.telegram_chat_id or '(none)'}")


def cmd_seed_demo_buyers(args: list[str]) -> None:
    from app.db.models import Geo
    from app.domain.criteria import CriteriaInput, FEATURES
    from app.services import buyers, criteria

    n = int(args[0]) if args else 50
    with session_scope() as session:
        councils = list(session.scalars(select(Geo.code).where(Geo.level == "council")))
        features = list(FEATURES)
        created = 0
        for i in range(1, n + 1):
            label = f"Demo {i:02d}"
            if session.scalar(select(buyers.Buyer).where(buyers.Buyer.label == label)):
                continue
            buyer = buyers.create(session, label)
            price_min = random.choice([150_000, 200_000, 300_000, 400_000])
            data = CriteriaInput(
                buyer_id=buyer.id,
                estate_types=[random.choice(["apartamento", "moradia"])],
                typology_min=random.randint(0, 3),
                typology_max=random.choice([None, 3, 4]),
                price_min=price_min,
                price_max=price_min + random.choice([100_000, 150_000, 250_000]),
                must_have=random.sample(features, 1),
                nice_to_have=random.sample(features, 1),
                zones=random.sample(councils, 3),
                source_text="(demo)",
            )
            if set(data.must_have) & set(data.nice_to_have):
                data.nice_to_have = []
            criteria.create(session, data, queue_initial_run=False)  # local test data: never crawl for demo buyers
            created += 1
        print(f"demo buyers created: {created}")


def cmd_delete_demo_buyers(_: list[str]) -> None:
    from app.db.models import Buyer

    with session_scope() as session:
        rows = list(session.scalars(select(Buyer).where(Buyer.label.like("Demo %"))))
        for row in rows:
            session.delete(row)
        print(f"demo buyers deleted: {len(rows)}")


def cmd_run_initial(args: list[str]) -> None:
    """run-initial <criteria_id>: queue and execute the targeted first pass for one criteria, now."""
    from app.db.models import Criteria
    from app.services import runs

    if not args:
        sys.exit("usage: run-initial <criteria_id>")
    with session_scope() as session:
        criteria = session.get(Criteria, int(args[0]))
        if criteria is None:
            sys.exit("criteria not found")
        for run in runs.queue_initial(session, criteria):
            runs.execute_initial(session, run)
            print(f"run {run.id} {run.status}: new={run.listings_new} updated={run.listings_updated} pages={run.pages_fetched} error={run.error}")


def cmd_run_queued(_: list[str]) -> None:
    from app.services import runs

    with session_scope() as session:
        print(f"queued runs executed: {runs.run_queued(session)}")


def cmd_sweep(args: list[str]) -> None:
    """sweep <source_key>: one incremental pass + matching + alerts, exactly what the worker does on schedule."""
    from app.services import runs

    if not args:
        sys.exit("usage: sweep <source_key>")
    with session_scope() as session:
        run = runs.execute_sweep(session, args[0])
        print(f"run {run.id} {run.status}: new={run.listings_new} updated={run.listings_updated} scope-queries={run.pages_fetched} error={run.error}")


def cmd_backfill(args: list[str]) -> None:
    """backfill <source_key> [limit]: the one-off historical pass over a catalogue source (RE/MAX).

    Resumable: run it again after a crash or a restart and it continues from `crawl_runs.cursor`. Silent by
    design - a bulk import of listings already on the market never alerts. Pass a `limit` to read only that
    many listings, which is how the pass is measured before the whole catalogue is committed to."""
    import time

    from app.services import runs

    if not args:
        sys.exit("usage: backfill <source_key> [limit]")
    limit = int(args[1]) if len(args) > 1 else None
    started = time.monotonic()
    with session_scope() as session:
        run = runs.execute_backfill(session, args[0], limit=limit)
        elapsed = time.monotonic() - started
        position = (run.cursor or {}).get("index") or 0
        total = (run.cursor or {}).get("total") or 0
        rate = position / elapsed if elapsed else 0
        print(f"run {run.id} {run.status}: new={run.listings_new} updated={run.listings_updated} "
              f"at {position}/{total} in {elapsed:.0f}s ({rate:.2f} listings/s) error={run.error}")
        # Only project from a slice big enough to mean anything: a 20-listing run that reused a cached
        # sitemap reports an absurd rate, and "0.1 h left" is the kind of number someone acts on at 2am.
        done_here = run.listings_new + run.listings_updated
        if rate and total and done_here >= 50:
            remaining = (total - position) / rate
            print(f"projected for the whole catalogue: {total / rate / 3600:.1f} h total, {remaining / 3600:.1f} h left")
        elif total:
            print(f"slice too small to project from ({done_here} listings); run at least 50 for a useful estimate")


def cmd_warm_hashes(args: list[str]) -> None:
    """warm-hashes [limit]: fill the photo hashes dedup compares, so a catalogue backfill is not paying to
    hash the other source's photos while it crawls. Idempotent; run it until `remaining` is 0."""
    from app.services import properties as prop_svc

    limit = int(args[0]) if args else 500
    with session_scope() as session:
        print(prop_svc.warm_hashes(session, limit=limit))


def cmd_matched_refresh(_: list[str]) -> None:
    """matched-refresh: re-read the detail page of every matched listing (price history, removals, extra fields)."""
    from app.services import refresh

    with session_scope() as session:
        print(refresh.refresh_matched(session))


def cmd_export(args: list[str]) -> None:
    """export [path.xlsx]: the panel's result list as a spreadsheet."""
    from app.services import export

    target = args[0] if args else export.filename()
    with session_scope() as session:
        data = export.workbook(session).getvalue()
    with open(target, "wb") as handle:
        handle.write(data)
    print(f"{target} ({len(data)} bytes)")


def cmd_flush_notifications(_: list[str]) -> None:
    from app.services import notifications

    with session_scope() as session:
        print(f"messages sent: {notifications.flush(session)}")


def cmd_rematch(_: list[str]) -> None:
    """rematch: re-evaluate every active criteria against every stored property. Silent (no alerts); run after a data
    correction (e.g. migration 0003) or when criteria were edited."""
    from app.services import runs

    with session_scope() as session:
        for criteria_id, count in runs.rematch_all(session).items():
            print(f"criteria {criteria_id}: {count} matching properties")


def cmd_poll_email(args: list[str]) -> None:
    """poll-email [--rescan] [--silent]: read the alert mailbox.

    `--rescan` rewinds the UID watermark and reads every message again, which is what you want after fixing
    the parser; `--silent` suppresses alerts, which is what you want when that backlog is months old."""
    from app.services import runs

    with session_scope() as session:
        run = runs.execute_email_poll(session, rescan="--rescan" in args, initial="--silent" in args)
        print(f"run {run.id} {run.status}: messages={run.pages_fetched} new={run.listings_new} updated={run.listings_updated} error={run.error}")


def cmd_split(args: list[str]) -> None:
    from app.services import properties

    if not args:
        sys.exit("usage: split <listing_id>")
    with session_scope() as session:
        prop = properties.split(session, int(args[0]))
        print(f"listing {args[0]} moved to its own property {prop.id}")


def cmd_rewind_watermarks(args: list[str]) -> None:
    """rewind-watermarks <source_key> <hours>: make the next sweep look further back (testing / after downtime)."""
    from app.services import runs

    if len(args) < 2:
        sys.exit("usage: rewind-watermarks <source_key> <hours>")
    with session_scope() as session:
        print(f"watermarks rewound: {runs.rewind_watermarks(session, args[0], float(args[1]))}")


def cmd_agency_directory(args: list[str]) -> None:
    from app.services import agencies

    with session_scope() as session:
        stats = agencies.crawl_directory(session, max_pages=int(args[0]) if args else None)
    print(f"mediadoras: {stats}")


def cmd_agency_websites(args: list[str]) -> None:
    from app.services import agencies

    with session_scope() as session:
        print(agencies.resolve_websites(session, limit=int(args[0]) if args else 100))


def cmd_agency_profile(args: list[str]) -> None:
    from app.services import agencies

    limit = int(args[0]) if args else 10
    with session_scope() as session:
        pending = agencies.pending_profiles(session, limit=limit)
        if not pending:
            print("nada para perfilar: ou já estão todas feitas, ou nenhuma tem site.")
            return
        for agency in pending:
            source = agencies.profile_agency(session, agency)
            session.commit()
            if source is None:
                continue
            profile = source.profile or {}
            print(f"{source.key:<34} {str(source.confidence):<8} {profile.get('platform'):<8} "
                  f"catalogo={(profile.get('index') or {}).get('catalogue_size')} "
                  f"inc={profile.get('incremental')} campos={','.join(profile.get('fields_present') or []) or '-'}"
                  + (f"  [{profile.get('notes')}]" if profile.get("notes") else ""))


def cmd_agency_watch(args: list[str]) -> None:
    from app.services import agencies

    limit = int(args[0]) if args else 10
    with session_scope() as session:
        due = agencies.due_for_watch(session, limit=limit)
        if not due:
            print("nenhuma mediadora perfilada e ativa.")
            return
        for source in due:
            first = source.last_ok_at is None
            run = agencies.watch(session, source, initial=first)
            ratio = agencies.refresh_offportal(session, source)
            session.commit()
            print(f"{source.key:<34} {run.status:<8} novos={run.listings_new:<5} "
                  f"lidos={run.pages_fetched:<5} {'(1a leitura, silenciosa)' if first else ''} "
                  f"fora-dos-portais={'-' if ratio is None else f'{ratio:.0%}'}  {run.cursor or {}}")


def cmd_agency_add(args: list[str]) -> None:
    from app.services import agencies

    if not args:
        sys.exit("usage: agency-add <url> [nome]")
    url = args[0]
    name = " ".join(args[1:]) or url.split("//")[-1].split("/")[0]
    with session_scope() as session:
        agency = agencies.add_manual_website(session, name, url)
        source = agencies.ensure_source(session, agency)
        session.commit()
        print(f"adicionada: {agency.name} -> {agency.website_url} (fonte {source.key if source else '-'})")


def cmd_agency_summary(_: list[str]) -> None:
    from app.services import agencies

    with session_scope() as session:
        data = agencies.summary(session)
    print(f"mediadoras encontradas       : {data['agencies']}")
    print(f"  com anuncios de venda      : {data['with_sale_ads']}")
    print(f"  perfis ja verificados      : {data['profiles_checked']}")
    print(f"  com site proprio           : {data['with_website']}")
    print(f"  legiveis (full|partial)    : {data['readable']}")
    print(f"  por confianca              : {data['by_confidence']}")


def cmd_agency_evidence(_: list[str]) -> None:
    from app.services import agencies

    with session_scope() as session:
        found = agencies.offportal_examples(session, limit=10)
        if not found:
            print("ainda sem provas: nenhum imovel de mediadora sem correspondencia num portal.")
            return
        for listing in found:
            print(f"- {listing.url}")
            print(f"    {listing.title or ''} | {listing.price} EUR | T{listing.rooms} | {listing.geo_code}")


COMMANDS = {
    "run-initial": cmd_run_initial,
    "run-queued": cmd_run_queued,
    "sweep": cmd_sweep,
    "backfill": cmd_backfill,
    "matched-refresh": cmd_matched_refresh,
    "warm-hashes": cmd_warm_hashes,
    "export": cmd_export,
    "flush-notifications": cmd_flush_notifications,
    "rewind-watermarks": cmd_rewind_watermarks,
    "rematch": cmd_rematch,
    "poll-email": cmd_poll_email,
    "split": cmd_split,
    "seed-geo": cmd_seed_geo,
    "seed-sources": cmd_seed_sources,
    "refresh-robots": cmd_refresh_robots,
    "hash-password": cmd_hash_password,
    "send-test": cmd_send_test,
    "seed-demo-buyers": cmd_seed_demo_buyers,
    "delete-demo-buyers": cmd_delete_demo_buyers,
    "agency-directory": cmd_agency_directory,
    "agency-websites": cmd_agency_websites,
    "agency-profile": cmd_agency_profile,
    "agency-watch": cmd_agency_watch,
    "agency-add": cmd_agency_add,
    "agency-summary": cmd_agency_summary,
    "agency-evidence": cmd_agency_evidence,
}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        sys.exit(__doc__)
    setup_logging(settings.log_level)
    COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main()
