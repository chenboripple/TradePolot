#!/usr/bin/env python3
"""
筛选近6个月涨幅超过50%且趋势强劲的股票
筛选条件：
1. 近6个月涨跌幅 > 50%
2. 近1个月涨跌幅 > 10%（近期仍在上涨）
3. 当前价格 > MA20（短期多头排列）
4. 成交量较前期有所放大

重点关注：
- 科技/AI相关（光模块、算力、芯片）
- 医药医疗（创新药、医疗器械）
- 新能源/新材料
- 其他高成长赛道
"""

import sys
import os
import typing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), './src'))

from datetime import datetime, timedelta
import pandas as pd
from pathlib import Path
from ripple_tradePilot.data.tushare_loader import TushareDataLoader

# 从环境变量或配置中获取Tushare Token
# 项目中已配置，这里直接使用示例中的token
TOKEN = "your_tushare_token_here"

# 重点关注行业关键词
FOCUS_INDUSTRIES = {
    '科技': ['半导体', '芯片', '光模块', '算力', '人工智能', 'AI', '软件', '互联网', '电子', '通信', '信息技术'],
    '医药': ['医药', '医疗', '创新药', '医疗器械', '生物', '疫苗', '医疗保健'],
    '新能源': ['新能源', '光伏', '风电', '储能', '电池', '锂电', '新材料', '氢能'],
}

def calculate_indicators(df: pd.DataFrame) -> typing.Optional[dict]:
    """
    计算所需技术指标
    """
    df = df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    
    if len(df) < 60:
        return None  # 数据不足
    
    # 当前价格
    current_close = df.iloc[-1]['close']
    
    # 近6个月涨跌幅（从第一条到最后一条）
    start_6m_price = df.iloc[0]['close']
    change_6m = ((current_close - start_6m_price) / start_6m_price) * 100
    
    # 近1个月涨跌幅（约22个交易日）
    if len(df) >= 22:
        start_1m_price = df.iloc[-22]['close'] if len(df) > 22 else df.iloc[0]['close']
        change_1m = ((current_close - start_1m_price) / start_1m_price) * 100
    else:
        change_1m = change_6m
    
    # 计算MA20
    df['ma20'] = df['close'].rolling(window=20).mean()
    ma20 = df.iloc[-1]['ma20']
    
    # 计算成交量放大：最近5日平均成交量 vs 之前20日平均成交量
    if len(df) >= 25:
        recent_5_vol = df.tail(5)['vol'].mean()
        prev_20_vol = df.iloc[-25:-5]['vol'].mean()
        vol_ratio = recent_5_vol / prev_20_vol if prev_20_vol > 0 else 1
    else:
        vol_ratio = 1
    
    return {
        'current_close': current_close,
        'change_6m': change_6m,
        'change_1m': change_1m,
        'ma20': ma20,
        'vol_ratio': vol_ratio,
        'data_len': len(df)
    }

def calculate_support_resistance(df: pd.DataFrame, n_levels: int = 3) -> tuple:
    """
    根据近期高点低点计算支撑位和压力位
    """
    df = df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    recent = df.tail(120)
    
    # 找局部低点（支撑位）
    lows = []
    for i in range(2, len(recent) - 2):
        if recent.iloc[i].low < recent.iloc[i-1].low and recent.iloc[i].low < recent.iloc[i-2].low and \
           recent.iloc[i].low < recent.iloc[i+1].low and recent.iloc[i].low < recent.iloc[i+2].low:
            lows.append(recent.iloc[i].low)
    
    # 排序取最近N个关键支撑位，保留离当前价格较近的
    current_price = recent.iloc[-1].close
    lows_below = [l for l in lows if l < current_price]
    lows_below = sorted(lows_below, reverse=True)  # 从高到低，最近的支撑位在前面
    
    # 如果数量不够，用百分比补充
    if len(lows_below) == 0:
        min_price = recent['low'].min()
        lows_below = [min_price]
    while len(lows_below) < n_levels:
        next_support = lows_below[-1] * 0.95
        if next_support not in lows_below:
            lows_below.append(next_support)
    
    return sorted(lows_below[:n_levels], reverse=True)  # 返回从高到低排序的支撑位

def judge_trend(indicators: dict) -> str:
    """判断当前趋势强度"""
    change_6m = indicators['change_6m']
    change_1m = indicators['change_1m']
    current_above_ma20 = indicators['current_close'] > indicators['ma20']
    vol_ratio = indicators['vol_ratio']
    
    if change_6m > 100 and change_1m > 15 and current_above_ma20 and vol_ratio > 1.2:
        return "超强多头趋势，上涨动能充足"
    elif change_6m > 50 and change_1m > 10 and current_above_ma20 and vol_ratio > 1.1:
        return "强劲多头趋势，趋势延续性好"
    elif change_6m > 50 and change_1m > 10 and current_above_ma20:
        return "多头趋势，走势偏强"
    elif change_6m > 50 and change_1m > 5 and current_above_ma20:
        return "多头趋势，但近期涨速放缓"
    else:
        return "趋势强度一般"

def get_investment_advice(current_price: float, supports: list, change_6m: float, change_1m: float, trend: str) -> str:
    """给出投资建议"""
    nearest_support = supports[0] if supports else None
    
    if not nearest_support:
        return "无明确支撑位，建议观望"
    
    distance_to_support = ((current_price - nearest_support) / nearest_support) * 100
    
    if "超强多头" in trend or "强劲多头" in trend:
        if distance_to_support > 8:
            return f"当前涨幅较大，价格距离最近支撑位({nearest_support:.2f})已有{distance_to_support:.1f}%涨幅，追涨风险较高，建议等待回踩支撑位后介入"
        elif distance_to_support > 3:
            return f"趋势强劲，可轻仓追涨，止损设置在{nearest_support:.2f}支撑位下方"
        else:
            return f"价格回踩关键支撑位{nearest_support:.2f}，处于上升趋势中，适合逢低分批介入"
    elif "多头趋势" in trend:
        if distance_to_support > 5:
            return f"不建议追涨，等待回调至{nearest_support:.2f}附近支撑位再考虑介入"
        else:
            return f"价格接近支撑位，趋势向好，可考虑分批低吸"
    else:
        return f"趋势强度一般，建议观望，等待更明确的入场信号"

def get_industry_category(industry: str) -> typing.Optional[str]:
    """判断股票是否属于重点关注行业"""
    if not industry:
        return None
    
    industry = industry.lower()
    for category, keywords in FOCUS_INDUSTRIES.items():
        for keyword in keywords:
            if keyword.lower() in industry:
                return category
    return None

def main():
    loader = TushareDataLoader(TOKEN)
    
    # 计算时间范围
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
    
    print("=" * 90)
    print("近6个月涨幅超50%强劲趋势股票筛选")
    print(f"开始时间: {datetime.now()}")
    print(f"时间范围: {start_date} - {end_date}")
    print("=" * 90)
    print()
    
    # 获取全部股票列表
    print("正在获取A股股票列表...")
    stock_list = loader.get_stock_list()
    print(f"共获取到 {len(stock_list)} 只上市股票")
    
    # 筛选出重点关注行业的股票
    focus_stocks = []
    for _, row in stock_list.iterrows():
        industry = row.get('industry', '')
        category = get_industry_category(str(industry))
        if category:
            focus_stocks.append({
                'ts_code': row['ts_code'],
                'name': row['name'],
                'industry': industry,
                'category': category
            })
    
    # 如果重点行业股票太多，限制数量避免超限
    if len(focus_stocks) > 300:
        print(f"筛选出 {len(focus_stocks)} 只重点行业股票，将随机抽取300只进行分析以控制API调用量")
        import random
        random.shuffle(focus_stocks)
        focus_stocks = focus_stocks[:300]
    
    print(f"开始分析 {len(focus_stocks)} 只重点行业股票...")
    print()
    
    results = []
    counter = 0
    total = len(focus_stocks)
    
    for stock in focus_stocks:
        counter += 1
        ts_code = stock['ts_code']
        name = stock['name']
        industry = stock['industry']
        category = stock['category']
        
        if counter % 50 == 0:
            print(f"已处理 {counter}/{total}...")
        
        # 获取日线数据
        df = loader.get_daily_bars(ts_code, start_date=start_date, end_date=end_date)
        
        if df is None or len(df) < 60:
            continue
        
        # 计算指标
        indicators = calculate_indicators(df)
        if not indicators:
            continue
        
        # 检查筛选条件
        if (indicators['change_6m'] > 50 and
            indicators['change_1m'] > 10 and
            indicators['current_close'] > indicators['ma20'] and
            indicators['vol_ratio'] > 1.0):  # 成交量不缩量即可，放大更好
            
            # 计算支撑位
            supports = calculate_support_resistance(df)
            
            # 判断趋势
            trend = judge_trend(indicators)
            
            # 投资建议
            advice = get_investment_advice(
                indicators['current_close'],
                supports,
                indicators['change_6m'],
                indicators['change_1m'],
                trend
            )
            
            results.append({
                'ts_code': ts_code,
                'name': name,
                'industry': industry,
                'category': category,
                'change_6m': indicators['change_6m'],
                'change_1m': indicators['change_1m'],
                'current_close': indicators['current_close'],
                'ma20': indicators['ma20'],
                'vol_ratio': indicators['vol_ratio'],
                'supports': supports,
                'trend': trend,
                'advice': advice
            })
    
    # 按6个月涨幅排序
    results.sort(key=lambda x: x['change_6m'], reverse=True)
    
    print()
    print("=" * 90)
    print(f"筛选完成，共找到 {len(results)} 只符合条件的股票")
    print("=" * 90)
    print()
    
    # 分类输出结果
    for category in FOCUS_INDUSTRIES.keys():
        category_results = [r for r in results if r['category'] == category]
        if category_results:
            print(f"## {category}板块（{len(category_results)}只）")
            print()
            for r in category_results:
                print(f"### {r['name']} ({r['ts_code']})")
                print(f"- **行业**: {r['industry']}")
                print(f"- **近6个月涨幅**: {r['change_6m']:.1f}%")
                print(f"- **近1个月涨幅**: {r['change_1m']:.1f}%")
                print(f"- **当前价格**: {r['current_close']:.2f} 元")
                print(f"- **MA20**: {r['ma20']:.2f} 元")
                print(f"- **最近5日成交量 vs 前期**: {r['vol_ratio']:.2f}倍")
                print(f"- **当前趋势**: {r['trend']}")
                print(f"- **关键支撑位**: {', '.join([f'{s:.2f}' for s in r['supports']])}")
                print(f"- **操作建议**: {r['advice']}")
                print()
    
    # 其他板块符合条件的
    other_results = [r for r in results if r['category'] not in FOCUS_INDUSTRIES.keys()]
    if other_results:
        print(f"## 其他板块（{len(other_results)}只）")
        print()
        for r in other_results:
            print(f"### {r['name']} ({r['ts_code']})")
            print(f"- **行业**: {r['industry']}")
            print(f"- **近6个月涨幅**: {r['change_6m']:.1f}%")
            print(f"- **近1个月涨幅**: {r['change_1m']:.1f}%")
            print(f"- **当前价格**: {r['current_close']:.2f} 元")
            print(f"- **关键支撑位**: {', '.join([f'{s:.2f}' for s in r['supports']])}")
            print(f"- **当前趋势**: {r['trend']}")
            print(f"- **操作建议**: {r['advice']}")
            print()
    
    # 保存结果到CSV
    output_dir = Path("./output")
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f"screen_result_{timestamp}.csv"
    
    # 转换为DataFrame保存
    result_df = pd.DataFrame([
        {
            '代码': r['ts_code'],
            '名称': r['name'],
            '板块': r['category'],
            '行业': r['industry'],
            '6个月涨幅%': round(r['change_6m'], 2),
            '1个月涨幅%': round(r['change_1m'], 2),
            '当前价格': round(r['current_close'], 2),
            'MA20': round(r['ma20'], 2),
            '成交量倍数': round(r['vol_ratio'], 2),
            '趋势': r['trend'],
            '第一支撑位': round(r['supports'][0], 2) if len(r['supports'])>0 else None,
            '第二支撑位': round(r['supports'][1], 2) if len(r['supports'])>1 else None,
            '第三支撑位': round(r['supports'][2], 2) if len(r['supports'])>2 else None,
            '操作建议': r['advice']
        } for r in results
    ])
    result_df.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"\n完整结果已保存到: {output_file}")
    
    print()
    print(f"筛选完成时间: {datetime.now()}")

if __name__ == "__main__":
    main()
