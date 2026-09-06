#!/usr/bin/env python3
"""
使用 AkShare 获取 A 股股票列表，并保存为 CSV
绕过 Tushare 接口限流
"""

import akshare as ak
import pandas as pd
from datetime import datetime

def main():
    print("正在使用 AkShare 获取 A 股股票列表...")
    
    # 获取 A 股股票列表
    df = ak.stock_info_a_code_name()
    
    # 获取更多信息（行业等）
    # AkShare 的 stock_info_a_code_name 只有代码和名称
    # 需要转换代码格式为 Tushare 格式
    def convert_code(code):
        if str(code).startswith('60') or str(code).startswith('688') or str(code).startswith('900'):
            return f"{code}.SH"
        elif str(code).startswith('00') or str(code).startswith('001') or str(code).startswith('30') or str(code).startswith('300'):
            return f"{code}.SZ"
        elif str(code).startswith('8') or str(code).startswith('4'):
            return f"{code}.BJ"
        else:
            return f"{code}.SZ"  # 默认归为深交所
    
    df['ts_code'] = df['code'].apply(lambda x: convert_code(str(x).zfill(6)))
    
    # 重命名列
    df = df.rename(columns={'name': 'name'})
    df['symbol'] = df['code']
    df['industry'] = ''  # 行业信息暂时留空
    
    print(f"共获取到 {len(df)} 只股票")
    
    # 排除北交所
    df = df[~df['ts_code'].str.endswith('.BJ')]
    print(f"排除北交所后剩余 {len(df)} 只股票")
    
    # 保存
    output_file = f"/Users/ripple/work space/ripple_tradePilot/data/stock_list_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"股票列表已保存到: {output_file}")
    
    # 也保存为固定文件名
    df.to_csv("/Users/ripple/work space/ripple_tradePilot/data/stock_list.csv", index=False, encoding='utf-8-sig')

if __name__ == "__main__":
    main()
