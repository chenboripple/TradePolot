"""
飞书机器人通知模块（Webhook 方式）

使用自定义机器人 Webhook 发送消息，支持签名校验。
（早期"自建应用"实现已移除：其 receive_id 永远无法配置，属于死代码。）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

import httpx

from ripple_tradePilot.models.types import Bar, Side


class FeishuWebhookNotifier:
    """飞书 Webhook 机器人通知器（支持签名校验）"""
    
    def __init__(self, webhook_url: str, secret: str = None,
                 dashboard_url: Optional[str] = None):
        self.webhook_url = webhook_url
        self.secret = secret
        # 可选：监控台地址，附在信号卡片底部便于一键跳转看图
        self.dashboard_url = dashboard_url
    
    def _generate_signature(self, timestamp: str) -> str:
        """生成签名（飞书签名校验）

        飞书机器人签名规则：
        - key = f"{timestamp}\n{secret}".encode("utf-8")
        - msg = b""
        - sign = base64(hmac_sha256(key, msg))
        """
        if not self.secret:
            return ""

        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    def _post(self, payload: dict) -> bool:
        """发送任意飞书 Webhook 消息；配置了 secret 时自动附加签名。"""
        try:
            body = dict(payload)
            if self.secret:
                timestamp = str(int(time.time()))
                body["timestamp"] = timestamp
                body["sign"] = self._generate_signature(timestamp)

            response = httpx.post(
                self.webhook_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            result = response.json()

            code = result.get("code", result.get("StatusCode", 0))
            if code != 0:
                print(f"飞书 Webhook 发送失败：{result}")
                return False

            print("✅ 飞书消息发送成功")
            return True

        except Exception as e:
            print(f"发送飞书通知异常：{e}")
            return False
    
    def send(self, symbol: str, name: str, side: Side, price: float,
             strategy: str, bar: Bar, extra_info: Optional[dict] = None) -> bool:
        """发送交易信号通知（带签名校验）。

        ``extra_info`` 可含 ``stop_loss`` / ``take_profit`` 提示价（按风控参数算出，
        仅供参考；本系统只读不下单）。
        """
        # 颜色与图标统一遵循全站 A 股口径：买/涨=红，卖/跌=绿
        # （与 Web 端 --buy:#ba1a1a、K 线红涨绿跌一致，避免红绿语义自相矛盾）
        color_map = {Side.BUY: "red", Side.SELL: "green"}
        icon_map = {Side.BUY: "🔴", Side.SELL: "🟢"}

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**标的：** {name} ({symbol})\n**信号：** {side.value}\n**价格：** {price:.2f} 元\n**策略：** {strategy}\n**时间：** {bar.timestamp.strftime('%Y-%m-%d %H:%M')}"
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**K 线详情：**\n• 开盘：{bar.open:.2f}\n• 最高：{bar.high:.2f}\n• 最低：{bar.low:.2f}\n• 收盘：{bar.close:.2f}\n• 成交量：{bar.volume/10000:.1f}万手"
                }
            },
        ]

        info = extra_info or {}

        # A3：盘中预估 / 收盘确认状态 + 触发组件注解（monitor 经 extra_info 传入；
        # 仅当调用方显式给出 provisional 键时才渲染状态行，保持对旧调用方向后兼容）
        status_lines = []
        if "provisional" in info:
            status_lines.append(
                "⏳ **盘中预估**（未收盘，以收盘确认为准）"
                if info.get("provisional")
                else "✅ **收盘确认**"
            )
        note = info.get("note")
        if note:
            status_lines.append(f"🔧 {note}")
        if status_lines:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(status_lines)},
            })

        # D5：ML 模型预估块（monitor 收盘例程经 extra_info['forecast'] 传入 ScoreResult.to_dict()）。
        # 缺字段就省略该行，绝不补 0——0.00% 会被读成"预测不涨不跌"，是撒谎。
        forecast = info.get("forecast")
        if forecast and forecast.get("p_win") is not None:
            metric_parts = [f"• 胜率（{forecast.get('horizon_days', 5)}日）："
                            f"**{forecast['p_win']:.1%}**"]
            if forecast.get("expected_net_return") is not None:
                metric_parts.append(
                    f"• 期望净收益：**{forecast['expected_net_return']:+.2%}**"
                )
            if forecast.get("downside_mae") is not None:
                metric_parts.append(f"• 参考下行：**{forecast['downside_mae']:.2%}**")
            head = "🧠 **模型预估**"
            if forecast.get("status") == "demo":
                head += "（演示模型，门禁未过，不可作为交易依据）"
            elif forecast.get("stale"):
                head += "（数据滞后，模型已标 stale）"
            forecast_lines = [head, *metric_parts]
            if forecast.get("return_basis"):
                forecast_lines.append(f"口径：{forecast['return_basis']}")
            forecast_lines.append(
                f"基准日 {forecast.get('as_of', '--')} · 模型 {forecast.get('model_id', '--')}"
            )
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(forecast_lines)},
            })

        # 风控提示价（仅买入信号有意义：给出参考止损/止盈位）
        stop_loss = info.get("stop_loss")
        take_profit = info.get("take_profit")
        if stop_loss is not None or take_profit is not None:
            risk_lines = ["**风控参考位：**"]
            if stop_loss is not None:
                risk_lines.append(f"• 止损：{stop_loss:.2f} 元")
            if take_profit is not None:
                risk_lines.append(f"• 止盈：{take_profit:.2f} 元")
            risk_lines.append("（按风控参数估算，仅供参考，不构成投资建议）")
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(risk_lines)}
            })

        # 监控台跳转链接（配置了 dashboard_url 时附上）
        if self.dashboard_url:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📈 打开监控台查看图表"},
                        "url": self.dashboard_url,
                        "type": "primary",
                    }
                ],
            })

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "⚠️ 投资有风险，入市需谨慎"}]
        })

        content = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{icon_map.get(side, '⚪')} 交易信号提醒"
                    },
                    "template": color_map.get(side, "blue")
                },
                "elements": elements,
            }
        }

        return self._post(content)

    def send_card(self, content: dict) -> bool:
        """发送已构建好的 interactive 卡片消息（监控定期报告等）。"""
        return self._post(content)

    def _send_text(self, content: dict) -> bool:
        """发送纯文本消息（monitor_brief / heartbeat 等根目录脚本在用）。"""
        return self._post(content)
