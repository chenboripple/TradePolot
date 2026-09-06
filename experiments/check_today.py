import requests
from datetime import datetime

def get_sina_quote(code):
    if code.endswith('.SZ'):
        sina_code = f'sz{code[:-3]}'
    elif code.endswith('.SH'):
        sina_code = f'sh{code[:-3]}'
    else:
        sina_code = code
    
    url = f'https://hq.sinajs.cn/list={sina_code}'
    headers = {'Referer': 'https://finance.sina.com.cn'}
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'gb2312'
        text = resp.text
        
        if 'hq_str_' in text:
            data_str = text.split('="')[1].split('";')[0]
            parts = data_str.split(',')
            if len(parts) >= 33:
                name = parts[0]
                open_p = float(parts[1])
                pre_close = float(parts[2])
                close = float(parts[3])
                high = float(parts[4])
                low = float(parts[5])
                volume = int(parts[8])
                change_pct = ((close - pre_close) / pre_close * 100) if pre_close else 0
                
                return {
                    'name': name,
                    'close': round(close, 2),
                    'open': round(open_p, 2),
                    'high': round(high, 2),
                    'low': round(low, 2),
                    'volume': int(volume / 100),
                    'pre_close': round(pre_close, 2),
                    'change_pct': round(change_pct, 2)
                }
    except Exception as e:
        print(f'Error: {e}')
    return None

symbols = [
    ('002022.SZ', '科华生物'),
    ('600309.SH', '万华化学'),
    ('603039.SH', '泛海微'),
    ('000868.SZ', '安凯客车'),
    ('000999.SZ', '华润三九')
]

now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
print('**TradePilot 今日收盘数据 (新浪财经)** 📊')
print(f'时间：{now_str}')
print()

for code, expected_name in symbols:
    data = get_sina_quote(code)
    if data:
        change_pct = data['change_pct']
        emoji = '🔴' if change_pct > 0 else '🟢' if change_pct < 0 else '⚪'  # A 股口径：红涨绿跌
        print(f"{emoji} **{data['name']}** ({code})")
        print(f"收盘价：{data['close']} | 涨跌：{change_pct:+.2f}%")
        print(f"开盘：{data['open']} | 最高：{data['high']} | 最低：{data['low']}")
        print(f"成交量：{data['volume']} 手")
        print()
    else:
        print(f'❌ 获取失败: {code}')
