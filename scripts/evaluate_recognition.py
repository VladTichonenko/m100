"""Read current catalog and evaluate the actual configured LLM. Never writes work data."""

import argparse
import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx

from m100_bot.config import Settings
from m100_bot.domain import UserError
from m100_bot.llm import LLM
from m100_bot.sheets import Sheets


async def run(args):
    settings = Settings()
    cases = json.loads(Path(args.cases).read_text())
    if args.limit:
        cases = cases[: args.limit]
    results = []
    async with httpx.AsyncClient(timeout=90) as http:
        snap = await Sheets(settings, http).snapshot(datetime.now(settings.tz).date())
        llm = LLM(settings, http)
        for case in cases:
            started = time.monotonic()
            actual, unresolved, error = {}, False, None
            try:
                lines = await llm.extract(case["text"], snap.positions)
                for line in lines:
                    if line.position_id is None or line.quantity is None or line.question:
                        unresolved = True
                    else:
                        actual[line.position_id] = (
                            actual.get(line.position_id, Decimal(0)) + line.quantity
                        )
            except UserError as exc:
                error = str(exc)
            expected = {key: Decimal(value) for key, value in case.get("expected", {}).items()}
            passed = (
                (unresolved and not actual and not error)
                if case.get("expect_unresolved")
                else (actual == expected and not unresolved and not error)
            )
            result = {
                "id": case["id"],
                "passed": passed,
                "seconds": round(time.monotonic() - started, 2),
                "actual": {key: str(value) for key, value in actual.items()},
                "unresolved": unresolved,
                "error": error,
            }
            results.append(result)
            print(("PASS" if passed else "FAIL"), case["id"], result, flush=True)
    summary = {
        "total": len(results),
        "passed": sum(r["passed"] for r in results),
        "results": results,
        "writes_performed": False,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(
        f"Result: {summary['passed']}/{summary['total']}. No Sheets/Bitrix/Telegram writes.",
        flush=True,
    )
    return summary["passed"] == summary["total"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="evals/recognition.json")
    parser.add_argument("--output", default="artifacts/recognition-results.json")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
