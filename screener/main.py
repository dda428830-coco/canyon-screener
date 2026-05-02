"""
Canyon Screener 主流程
三层漏斗：宇宙池 → Module 0 → Canyon 评分 → Telegram 推送
"""
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

from screener.canyon_score import score_ticker
from screener.module0 import _bulk_download, run_module0
from screener.notifier import send_error, send_summary
from screener.universe import build_universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=" * 50)
    logger.info("Canyon Screener starting")
    logger.info("=" * 50)

    try:
        # ── 第一层：宇宙池 ──────────────────────────────────────────────────
        universe = build_universe()
        if not universe:
            msg = "宇宙池为空，可能是 Wikipedia 数据抓取失败。"
            logger.error(msg)
            send_error(msg)
            return

        # ── 第二层：Module 0 扫描 ────────────────────────────────────────────
        m0_results = run_module0(universe)
        if not m0_results:
            logger.warning("Module 0 无结果，今日无候选股票。")
            send_summary({"buy": [], "review": [], "watch": []})
            return

        # ── 第三层：Canyon 评分 ──────────────────────────────────────────────
        logger.info(f"Canyon scoring {len(m0_results)} candidates…")

        # 批量预取价格历史（复用 Module 0 的 bulk downloader）
        tickers   = [r["ticker"] for r in m0_results]
        hist_map  = _bulk_download(tickers)

        s0_map = {r["ticker"]: r for r in m0_results}

        from screener.config import SCORE_WORKERS
        scored: list[dict] = []

        def _score_worker(t: str) -> dict | None:
            return score_ticker(
                t,
                s0_map[t],
                universe.get(t, {}),
                hist_map.get(t),
            )

        with ThreadPoolExecutor(max_workers=SCORE_WORKERS) as ex:
            futures = {ex.submit(_score_worker, t): t for t in tickers}
            for fut in as_completed(futures):
                result = fut.result()
                if result and result["classification"] != "exclude":
                    scored.append(result)

        # ── 排序（可买 > 复核 > 观察；同池按 C 分降序）──────────────────────
        _priority = {"buy": 0, "review": 1, "watch": 2}
        scored.sort(key=lambda x: (_priority.get(x["classification"], 3),
                                   -x["c"].get("total", 0)))

        results_by_pool: dict[str, list[dict]] = {"buy": [], "review": [], "watch": []}
        for r in scored:
            pool = r["classification"]
            if pool in results_by_pool:
                results_by_pool[pool].append(r)

        logger.info(
            f"Final: buy={len(results_by_pool['buy'])}  "
            f"review={len(results_by_pool['review'])}  "
            f"watch={len(results_by_pool['watch'])}"
        )

        # ── 推送 Telegram ────────────────────────────────────────────────────
        send_summary(results_by_pool)
        logger.info("Canyon Screener done.")

    except Exception:
        tb = traceback.format_exc()
        logger.error(f"Fatal error:\n{tb}")
        send_error(f"扫描异常终止:\n{tb[-800:]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
