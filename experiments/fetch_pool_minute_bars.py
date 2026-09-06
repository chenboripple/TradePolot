#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
import sys
sys.path.insert(0, str(ROOT / 'src'))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader


def main() -> None:
    parser = argparse.ArgumentParser(description='串行抓取池子股票分钟线，优先 AkShare，失败回退 Tushare。')
    parser.add_argument('--freq', default='1min', help='分钟线频率: 1min/5min/15min/30min/60min')
    parser.add_argument('--days', type=int, default=5, help='回看天数')
    parser.add_argument('--sleep-seconds', type=float, default=35.0, help='每只股票之间的等待秒数，默认 35 秒')
    parser.add_argument('--symbols', nargs='*', help='可选：仅抓指定股票代码，如 000999.SZ 600309.SH')
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    loader = TushareDataLoader(config['tushare']['token'], rate_limit_delay=config['tushare'].get('rate_limit_delay', 1.5))

    out_dir = ROOT / 'data' / 'minute'
    out_dir.mkdir(parents=True, exist_ok=True)

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    selected = set(args.symbols or [])
    symbols = [s for s in config.get('symbols', []) if not selected or s['code'] in selected]

    print(f'开始抓取分钟线：freq={args.freq}, days={args.days}, symbols={len(symbols)}, sleep={args.sleep_seconds}s')
    print(f'时间范围：{start_dt:%Y-%m-%d %H:%M:%S} -> {end_dt:%Y-%m-%d %H:%M:%S}')
    print('-' * 80)

    for idx, symbol_cfg in enumerate(symbols, 1):
        code = symbol_cfg['code']
        name = symbol_cfg.get('name', code)
        print(f'[{idx}/{len(symbols)}] {name} ({code}) ...')
        try:
            df = loader.get_minute_bars(
                code,
                start_dt=start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                end_dt=end_dt.strftime('%Y-%m-%d %H:%M:%S'),
                freq=args.freq,
            )
            out_path = out_dir / f'{code}_{args.freq}.csv'
            if len(df) > 0:
                df.to_csv(out_path, index=False)
                print(f'  ✓ rows={len(df)} range={df.iloc[0]["datetime"]} -> {df.iloc[-1]["datetime"]}')
                print(f'  ✓ saved={out_path}')
            else:
                print('  ✗ no data')
        except Exception as e:
            print(f'  ✗ failed: {e}')

        if idx < len(symbols):
            print(f'  ...sleep {args.sleep_seconds}s to avoid minute-limit')
            time.sleep(args.sleep_seconds)

    print('-' * 80)
    print('抓取完成')


if __name__ == '__main__':
    main()
