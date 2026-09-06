#!/usr/bin/env python3
"""测试 Tushare Token 是否有效"""

import tushare as ts

# 设置 Token
TS_TOKEN = "your_tushare_token_here"
ts.set_token(TS_TOKEN)

# 初始化 API
pro = ts.pro_api()

# 测试 1: 获取用户积分
try:
    # 获取基本信息（测试连接）
    df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,market,list_date')
    print("✅ Token 有效！")
    print(f"\n📊 获取到 {len(df)} 只股票信息")
    print(f"\n前 5 只股票:")
    print(df.head())
except Exception as e:
    print(f"❌ Token 无效或 API 调用失败：{e}")
    exit(1)

# 测试 2: 获取科华生物日线数据
try:
    df = pro.daily(ts_code='002022.SZ', start_date='20260101', end_date='20260312')
    print(f"\n✅ 成功获取科华生物 (002022.SZ) 数据")
    print(f"   数据条数：{len(df)}")
    print(f"\n最近 5 个交易日:")
    print(df[['trade_date', 'open', 'high', 'low', 'close', 'volume']].head())
except Exception as e:
    print(f"⚠️ 获取日线数据失败：{e}")

# 测试 3: 获取账户积分信息（需要 2000 积分权限）
try:
    # 这个接口可能需要更高权限
    df = pro.trade_cal(exchange='SSE', start_date='20260301', end_date='20260331', is_open='1')
    print(f"\n✅ 成功获取交易日历数据")
except Exception as e:
    print(f"\n⚠️ 交易日历获取失败（可能积分不足）: {e}")

print("\n" + "="*50)
print("🎉 Tushare 测试完成！")
print("="*50)
