from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .agent import MerchantProfileAgent
from .api import create_server
from .crm import create_crm_resolver
from .knowledge_store import KnowledgeStoreUnavailable, create_knowledge_store
from .openai_client import OpenAIResponsesClient
from .settings import ConfigurationError, Settings, load_env_file
from .url_reader import PublicUrlReader


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the PiSell merchant profile agent API")
    parser.add_argument("--env-file", type=Path, help="Optional KEY=VALUE environment file")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.env_file:
            load_env_file(args.env_file)
        settings = Settings.from_env()
    except ConfigurationError as exc:
        parser.error(str(exc))

    client = OpenAIResponsesClient(
        api_key=settings.openai_api_key,
        model=settings.model,
        timeout=settings.openai_timeout,
    )
    try:
        knowledge_store = create_knowledge_store(settings)
    except KnowledgeStoreUnavailable as exc:
        parser.error(str(exc))
    crm_resolver = create_crm_resolver(settings)
    url_reader = None
    if settings.url_reading_enabled:
        url_reader = PublicUrlReader(
            timeout=settings.url_read_timeout,
            maximum_urls=settings.url_read_max_urls,
            maximum_bytes=settings.url_read_max_bytes,
            maximum_chars=settings.url_read_max_chars,
        )
    server = create_server(
        settings,
        MerchantProfileAgent(client, url_reader=url_reader),
        knowledge_store,
        crm_resolver,
    )
    logging.getLogger("merchant_profile_agent").info(
        "listening on http://%s:%d model=%s knowledge_store=%s crm_resolver=%s url_reader=%s",
        settings.host,
        settings.port,
        settings.model,
        knowledge_store.name,
        crm_resolver.name,
        "enabled" if settings.url_reading_enabled else "disabled",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
