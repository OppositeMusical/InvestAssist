"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .data import BarStore, ProviderError, build_provider
from .engine import Engine
from .score import InsufficientData
from .spec import Spec, SpecError

REPO_THINKSCRIPT = Path(__file__).resolve().parent.parent / "thinkscript"


def _add_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider", default="yfinance", help="yfinance, csv or stooq (stooq is unreliable)"
    )
    parser.add_argument("--csv-dir", help="directory of <SYMBOL>.csv files, for --provider csv")
    parser.add_argument("--spec", help="path to a spec YAML (defaults to the bundled one)")
    parser.add_argument("--cache", help="bar cache directory")


def _build_engine(args) -> Engine:
    spec = Spec.load(args.spec)
    provider = build_provider(args.provider, args.csv_dir)
    store = BarStore(provider, args.cache) if args.cache else BarStore(provider)
    return Engine(store, spec, equity=getattr(args, "equity", None))


def cmd_gen_thinkscript(args) -> int:
    from .thinkscript_gen import GeneratorError, generate

    spec = Spec.load(args.spec)
    directory = Path(args.dir) if args.dir else REPO_THINKSCRIPT
    try:
        changed = generate(spec, directory)
    except GeneratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if changed:
        print(f"regenerated {len(changed)} file(s) from spec '{spec.name}':")
        for name in changed:
            print(f"  {directory / name}")
    else:
        print("thinkScript files already match the spec")
    return 0


def cmd_score(args) -> int:
    engine = _build_engine(args)
    exit_code = 0
    for symbol in args.symbols:
        try:
            print(engine.recommend(symbol, refresh=args.refresh).as_text())
            print()
        except InsufficientData as exc:
            print(f"{symbol}: {exc}", file=sys.stderr)
            exit_code = 1
        except ProviderError as exc:
            print(f"{symbol}: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


def cmd_scan(args) -> int:
    engine = _build_engine(args)
    symbols = list(args.symbols)
    if args.file:
        symbols += [
            line.strip().upper()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not symbols:
        print("error: give some symbols, or --file", file=sys.stderr)
        return 1

    results = engine.scan(symbols)
    if not results:
        print("nothing could be scored")
        return 1

    print(f"{'symbol':<8}{'verdict':<12}{'score':>7}{'conf':>7}{'price':>10}{'stop':>10}")
    print("-" * 54)
    for rec in results:
        a = rec.assessment
        if args.min_score is not None and a.score < args.min_score:
            continue
        print(
            f"{a.symbol:<8}{a.verdict.value:<12}{a.score:>7.0f}{a.confidence:>6.0f}%"
            f"{a.price:>10,.2f}{a.exits.initial_stop:>10,.2f}"
        )
    return 0


def cmd_panel(args) -> int:
    from .panel import launch

    engine = _build_engine(args)
    launch(engine, topmost=not args.no_topmost)
    return 0


def cmd_backtest(args) -> int:
    from .backtest import run_backtest, slippage_sweep, walk_forward

    engine = _build_engine(args)
    spec = engine.spec

    symbols = list(args.symbols)
    if args.file:
        symbols += [
            line.strip().upper()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not symbols:
        print("error: give some symbols, or --file", file=sys.stderr)
        return 1

    bars = {}
    for symbol in symbols:
        try:
            bars[symbol] = engine.store.get(symbol, lookback_days=args.lookback_days)
        except Exception as exc:
            print(f"skipping {symbol}: {exc}", file=sys.stderr)
    if not bars:
        print("error: no usable data", file=sys.stderr)
        return 1

    benchmark = engine.benchmark()

    if args.sweep:
        print("Slippage sensitivity — if the edge dies here, it was never there.\n")
        print(slippage_sweep(bars, spec, benchmark, args.equity or 100_000).to_string())
        return 0

    if args.walk_forward:
        folds = walk_forward(bars, spec, benchmark, folds=args.folds,
                             initial_equity=args.equity or 100_000)
        print(f"{'fold':<6}{'test window':<26}{'trades':>7}{'expectancy':>12}{'return':>10}")
        print("-" * 61)
        for i, fold in enumerate(folds, start=1):
            m = fold.test_metrics
            if not m:
                print(f"{i:<6}{fold.test_start} .. {fold.test_end}   (unusable)")
                continue
            print(
                f"{i:<6}{str(fold.test_start) + ' .. ' + str(fold.test_end):<26}"
                f"{m['trades']:>7.0f}{m['expectancy_r']:>+12.3f}{m['total_return']:>+10.1%}"
            )
        return 0

    result = run_backtest(bars, spec, benchmark, initial_equity=args.equity or 100_000)
    print(f"{len(bars)} symbols, {result.equity_curve.index[0].date()} .. "
          f"{result.equity_curve.index[-1].date()}, slippage {result.slippage_bps} bps\n")
    print(result.summary())
    if result.metrics.get("trades", 0) < 30:
        print("\nwarning: fewer than 30 trades — this sample says almost nothing yet.")
    return 0


def cmd_record(args) -> int:
    from .recorder import BarRecorder
    from .rtd import build_client

    client = build_client(prefer_mock=args.mock)
    recorder = BarRecorder(
        client=client,
        symbols=args.symbols,
        directory=Path(args.out),
        interval_seconds=args.interval,
    )
    recorder.run(duration_seconds=args.duration)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="investassist",
        description="Buy/sell decision assist for US stocks, alongside thinkorswim",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen-thinkscript", help="regenerate the thinkScript studies from the spec")
    gen.add_argument("--spec")
    gen.add_argument("--dir", help="thinkscript output directory")
    gen.set_defaults(func=cmd_gen_thinkscript)

    score = sub.add_parser("score", help="score one or more symbols")
    score.add_argument("symbols", nargs="+")
    score.add_argument("--equity", type=float, help="account equity, to size the position")
    score.add_argument("--refresh", action="store_true", help="bypass the bar cache")
    _add_data_args(score)
    score.set_defaults(func=cmd_score)

    scan = sub.add_parser("scan", help="rank a universe")
    scan.add_argument("symbols", nargs="*")
    scan.add_argument("--file", help="file with one symbol per line")
    scan.add_argument("--min-score", type=float)
    _add_data_args(scan)
    scan.set_defaults(func=cmd_scan)

    panel = sub.add_parser("panel", help="launch the always-on-top companion panel")
    panel.add_argument("--equity", type=float)
    panel.add_argument("--no-topmost", action="store_true")
    _add_data_args(panel)
    panel.set_defaults(func=cmd_panel)

    bt = sub.add_parser("backtest", help="validate the strategy")
    bt.add_argument("symbols", nargs="*")
    bt.add_argument("--file")
    bt.add_argument("--equity", type=float, default=100_000)
    bt.add_argument("--lookback-days", type=int, default=2000)
    bt.add_argument("--sweep", action="store_true", help="slippage sensitivity table")
    bt.add_argument("--walk-forward", action="store_true", help="out-of-sample by fold")
    bt.add_argument("--folds", type=int, default=4)
    _add_data_args(bt)
    bt.set_defaults(func=cmd_backtest)

    rec = sub.add_parser("record", help="record RTD quotes into intraday bars")
    rec.add_argument("symbols", nargs="+")
    rec.add_argument("--out", default="./recorded", help="output directory")
    rec.add_argument("--interval", type=int, default=60, help="bar length in seconds")
    rec.add_argument("--duration", type=float, help="stop after this many seconds")
    rec.add_argument("--mock", action="store_true", help="use the mock RTD client")
    rec.set_defaults(func=cmd_record)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (SpecError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
