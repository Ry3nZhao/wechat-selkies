"""Entry point: ``python -m wechat_api`` (started by the s6 service svc-wechat-api)."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from .config import Config
from .gateway import Gateway
from .server import build_app, reverse_ws_loop
from .ui import UiDriver, make_x_runner


async def amain() -> None:
    cfg = Config.from_env()
    log = logging.getLogger("wechat_api")
    if not cfg.access_token:
        log.warning("WECHAT_API_TOKEN is not set: the OneBot HTTP/WebSocket endpoints stay closed")
    ui = UiDriver(cfg.ui, make_x_runner(cfg.display), screenshot_dir=cfg.screenshot_dir)
    gw = Gateway(cfg, ui=ui)
    tasks = await gw.start()
    tasks.append(asyncio.create_task(reverse_ws_loop(gw), name="reverse-ws"))
    runner = web.AppRunner(build_app(gw), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    log.info("wechat-api listening on %s:%s", cfg.host, cfg.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
