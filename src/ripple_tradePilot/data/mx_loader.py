"""
东方财富妙想数据加载器
基于 MX_APIKEY 获取分钟级实时数据
"""

from __future__ import annotations

import os
import re
import time
from datetime import timedelta
from typing import Iterable, Optional, Dict, Any

import pandas as pd
import requests

from ripple_tradePilot.data.cleaning import reject_price_outliers
from ripple_tradePilot.models.types import Bar


class MXDataLoader:
    """东方财富妙想数据加载器：支持分钟级数据获取"""
    
    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
    
    def __init__(self, api_key: Optional[str] = None, rate_limit_delay: float = 0.5):
        """
        初始化数据加载器
        
        Args:
            api_key: 妙想 API Key，默认从环境变量 MX_APIKEY 读取
            rate_limit_delay: 请求间隔秒数，默认 0.5 秒
        """
        self.api_key = api_key or os.getenv("MX_APIKEY")
        if not self.api_key:
            raise ValueError(
                "MX_APIKEY 环境变量未设置，请先设置环境变量：\n"
                "export MX_APIKEY=your_api_key_here"
            )
        self._rate_limit_delay = rate_limit_delay
        self._last_request_time = 0
    
    def _rate_limit(self):
        """限流保护"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.time()
    
    def _query(self, tool_query: str) -> Dict[str, Any]:
        """向妙想 API 发送查询请求"""
        self._rate_limit()
        
        headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key
        }
        data = {"toolQuery": tool_query}
        
        response = requests.post(self.BASE_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        return response.json()
    
    def _extract_price_data(self, result: Dict[str, Any]) -> Optional[pd.DataFrame]:
        """从 API 响应中提取价格数据为 DataFrame"""
        if result.get("status") != 0:
            return None
        
        data = result.get("data", {}).get("data", {})
        search_result = data.get("searchDataResultDTO", {})
        dto_list = search_result.get("dataTableDTOList", [])
        
        if not dto_list:
            return None
        
        # 取第一个数据表
        dto = dto_list[0]
        table = dto.get("table", {})
        name_map = dto.get("nameMap", {})
        
        if not isinstance(table, dict):
            return None
        
        # 提取日期/时间和价格数据
        headers = table.get("headName", [])  # 日期/时间列
        if not headers:
            return None
        
        # 构建 DataFrame
        rows = []
        for i, date_val in enumerate(headers):
            row = {"datetime": date_val}
            
            # 遍历所有指标列
            for key, values in table.items():
                if key == "headName":
                    continue
                
                # 获取指标名称
                label = name_map.get(key, key)
                if isinstance(label, (int, float)):
                    label = str(label)
                
                # 获取对应值
                value = values[i] if i < len(values) else None
                row[label] = value
            
            rows.append(row)
        
        df = pd.DataFrame(rows)
        
        # 解析 datetime - 处理多种格式
        # 格式1: "2025-04-07(日)" - 带星期
        # 格式2: "2025-04-07 10:30" - 带时间
        # 格式3: "2025-04-07" - 纯日期
        def parse_datetime(val):
            if pd.isna(val):
                return None
            val_str = str(val)
            # 移除星期部分，如 "(日)", "(一)" 等
            val_str = re.sub(r'\([^)]+\)', '', val_str).strip()
            try:
                return pd.to_datetime(val_str)
            except:
                return None
        
        df['datetime'] = df['datetime'].apply(parse_datetime)
        df = df.dropna(subset=['datetime'])
        
        # 标准化列名（映射常见的中文列名）
        column_mapping = {
            '最新价': 'close',
            '收盘价': 'close',
            '开盘价': 'open',
            '最高价': 'high',
            '最低价': 'low',
            '成交量': 'vol',
            '成交额': 'amount',
            '收盘': 'close',
            '开盘': 'open',
            '最高': 'high',
            '最低': 'low',
            'close': 'close',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'volume': 'vol',
            'vol': 'vol',
        }
        
        df = df.rename(columns=column_mapping)
        
        # 清洗数值数据（移除单位如 "元", "%", "," 等）
        def clean_numeric(val):
            if pd.isna(val):
                return None
            val_str = str(val)
            # 移除常见单位字符
            val_str = val_str.replace('元', '').replace('%', '').replace(',', '')
            try:
                return float(val_str)
            except:
                return None
        
        # 确保数值列为 float
        for col in ['open', 'high', 'low', 'close', 'vol', 'amount']:
            if col in df.columns:
                df[col] = df[col].apply(clean_numeric)
        
        return df.sort_values('datetime').reset_index(drop=True)
    
    def get_minute_bars(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        freq: str = "1min"
    ) -> pd.DataFrame:
        """
        获取分钟级行情数据
        
        Args:
            symbol: 股票代码，如 "300059.SZ" 或 "东方财富"
            start_date: 开始日期，格式 "YYYYMMDD"
            end_date: 结束日期，格式 "YYYYMMDD"
            freq: 频率，支持 "1min", "5min"（通过查询语句控制）
        
        Returns:
            DataFrame with columns: datetime, open, high, low, close, vol
        """
        # 构建自然语言查询
        if start_date and end_date:
            query = f"{symbol} {start_date} 到 {end_date} 每分钟最新价 开盘价 最高价 最低价 成交量"
        elif start_date:
            query = f"{symbol} 从 {start_date} 开始每分钟最新价 开盘价 最高价 最低价 成交量"
        else:
            # 默认获取最近交易日数据
            query = f"{symbol} 今天每分钟最新价 开盘价 最高价 最低价 成交量"
        
        result = self._query(query)
        df = self._extract_price_data(result)
        
        if df is None or len(df) == 0:
            return pd.DataFrame()
        
        # 过滤日期范围
        if start_date:
            start_dt = pd.to_datetime(start_date, format='%Y%m%d')
            df = df[df['datetime'] >= start_dt]
        
        if end_date:
            end_dt = pd.to_datetime(end_date, format='%Y%m%d') + timedelta(days=1)
            df = df[df['datetime'] < end_dt]
        
        # 重采样到目标频率（如果需要）
        if freq != "1min" and len(df) > 0:
            df = self._resample_bars(df, freq)
        
        return df.reset_index(drop=True)
    
    def _resample_bars(self, df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """重采样分钟数据到目标频率"""
        # 将 freq 转换为 pandas 频率字符串
        freq_map = {
            "1min": "1min",
            "5min": "5min",
            "15min": "15min",
            "30min": "30min",
            "60min": "60min",
        }
        pd_freq = freq_map.get(freq, "1min")
        
        df = df.set_index('datetime')
        
        resampled = df.resample(pd_freq).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'vol': 'sum',
            'amount': 'sum' if 'amount' in df.columns else 'sum',
        }).dropna()
        
        resampled = resampled.reset_index()
        return resampled
    
    def get_daily_bars(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        获取日线数据
        
        由于妙想 API 的自然语言查询限制，需要分别查询每个价格字段然后合并
        
        Args:
            symbol: 股票代码，如 "300059.SZ" 或 "东方财富"
            start_date: 开始日期，格式 "YYYYMMDD"
            end_date: 结束日期，格式 "YYYYMMDD"
        
        Returns:
            DataFrame with columns: datetime, open, high, low, close, vol
        """
        # 构建日期范围字符串
        if start_date and end_date:
            date_range = f"{start_date} 到 {end_date}"
        elif start_date:
            date_range = f"从 {start_date} 开始"
        else:
            date_range = "最近30天"
        
        # 分别查询各个价格字段
        price_fields = ['open', 'high', 'low', 'close']
        field_queries = {
            'open': f"{symbol} {date_range} 每天开盘价",
            'high': f"{symbol} {date_range} 每天最高价",
            'low': f"{symbol} {date_range} 每天最低价",
            'close': f"{symbol} {date_range} 每天收盘价",
        }
        
        merged_df = None
        
        for field, query in field_queries.items():
            result = self._query(query)
            df = self._extract_price_data(result)
            
            if df is None or len(df) == 0:
                continue
            
            # 重命名价格列为字段名
            price_cols = [c for c in df.columns if c not in ['datetime', 'vol', 'amount']]
            if price_cols:
                df = df.rename(columns={price_cols[0]: field})
            
            # 保留 datetime 和当前字段
            df = df[['datetime', field]]
            
            if merged_df is None:
                merged_df = df
            else:
                merged_df = merged_df.merge(df, on='datetime', how='outer')
        
        if merged_df is None or len(merged_df) == 0:
            return pd.DataFrame()
        
        # 按日期排序
        merged_df = merged_df.sort_values('datetime').reset_index(drop=True)
        
        # 确保所有价格字段都存在
        for field in price_fields:
            if field not in merged_df.columns:
                merged_df[field] = None
        
        # 查询成交量（可选）
        try:
            vol_query = f"{symbol} {date_range} 每天成交量"
            vol_result = self._query(vol_query)
            vol_df = self._extract_price_data(vol_result)
            if vol_df is not None and len(vol_df) > 0:
                vol_cols = [c for c in vol_df.columns if c not in ['datetime']]
                if vol_cols:
                    vol_df = vol_df.rename(columns={vol_cols[0]: 'vol'})
                    vol_df = vol_df[['datetime', 'vol']]
                    merged_df = merged_df.merge(vol_df, on='datetime', how='left')
        except:
            pass
        
        return merged_df

    def load_bars(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Iterable[Bar]:
        """
        加载日线为 Bar 迭代器（兼容回测引擎）
        """
        df = self.get_daily_bars(symbol, start_date, end_date)

        parsed: list = []
        for _, row in df.iterrows():
            try:
                # 处理 None 值
                open_price = row.get('open')
                high_price = row.get('high')
                low_price = row.get('low')
                close_price = row.get('close')
                volume = row.get('vol', 0)

                # 跳过任何价格为 None 的数据
                if open_price is None or high_price is None or low_price is None or close_price is None:
                    continue

                parsed.append(Bar(
                    timestamp=row['datetime'].to_pydatetime() if hasattr(row['datetime'], 'to_pydatetime') else row['datetime'],
                    open=float(open_price),
                    high=float(high_price),
                    low=float(low_price),
                    close=float(close_price),
                    volume=float(volume) if volume is not None else 0.0,
                ))
            except Exception as e:
                print(f"解析 K 线失败：{row}, 错误：{e}")
                continue

        # 量纲脏数据防护：妙想自然语言接口偶发返回与真实价相差数量级的价格
        yield from reject_price_outliers(parsed)
    
    def load_minute_bars(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        freq: str = "1min",
    ) -> Iterable[Bar]:
        """
        加载分钟线为 Bar 迭代器（用于实时监控）
        """
        df = self.get_minute_bars(symbol, start_date, end_date, freq)

        parsed: list = []
        for _, row in df.iterrows():
            try:
                parsed.append(Bar(
                    timestamp=row['datetime'].to_pydatetime() if hasattr(row['datetime'], 'to_pydatetime') else row['datetime'],
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=float(row.get('vol', 0)),
                ))
            except Exception as e:
                print(f"解析分钟 K 线失败：{row}, 错误：{e}")
                continue

        yield from reject_price_outliers(parsed)


if __name__ == "__main__":
    # 测试代码
    print("🧪 测试东方财富妙想数据加载器...")
    
    try:
        loader = MXDataLoader()
        
        # 测试日线数据
        print("\n📈 测试日线数据：东方财富 (300059.SZ)")
        df_daily = loader.get_daily_bars("300059.SZ", start_date="20250401", end_date="20250407")
        print(f"   获取到 {len(df_daily)} 条日线数据")
        if len(df_daily) > 0:
            print(df_daily[['datetime', 'open', 'high', 'low', 'close', 'vol']].head())
        
        # 测试分钟数据
        print("\n⏱️ 测试分钟数据：东方财富 (300059.SZ)")
        df_minute = loader.get_minute_bars("300059.SZ", start_date="20250407", end_date="20250407")
        print(f"   获取到 {len(df_minute)} 条分钟数据")
        if len(df_minute) > 0:
            print(df_minute[['datetime', 'open', 'high', 'low', 'close', 'vol']].head(10))
        
        # 测试 Bar 迭代器
        print("\n🔁 测试 Bar 迭代器...")
        bars = list(loader.load_minute_bars("300059.SZ", start_date="20250407", end_date="20250407"))
        print(f"   共 {len(bars)} 个 Bar")
        if bars:
            print(f"   最新 Bar: 时间={bars[-1].timestamp}, 收盘价={bars[-1].close}")
        
        print("\n✅ 测试完成！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
