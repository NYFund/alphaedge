import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution.order_preprocess import resolve_close_action
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.models import BaseOrder
from core.portfolio.aggregation import resolve_symbol_conflicts

"""
跨策略衝突守門：同一標的同時只允許一支策略持有（先搶先贏）

允許反向的話，台股同日同標的一買一賣會被券商**判成當沖**——稅率與成本跟回測不同、
還會吃掉當沖額度，而且兩邊部位在券商端互相沖銷，本地卻各自以為持有。

這道守門同時解掉三個問題：券商端反向沖銷、被判當沖、歸屬帳一對多的拆分。

**代價是預期的**：策略選股重疊時會有一支吃不到，實盤績效因此低於各自單跑的回測。
盤後的 parity 比對要把它歸成專屬類別，**不可混進「未解釋」**——
混進去會讓真正的未解釋差異被雜訊淹沒。

**判定在 `core/portfolio/aggregation.py`**（純函式），這裡只負責查歸屬帳、
查未終結委託、寫風控事件。
"""

# parity 比對用的歸因類別；被這道守門擋下的差異一律歸在這裡
CROSS_STRATEGY_BLOCKED: str = "CROSS_STRATEGY_BLOCKED"


class CrossStrategyConflictGuard:
    """
    - Description:
        同標的守門

        掛在委託前處理之後、批次曝險試算與逐單風控之前——**要在批次曝險試算前
        擋掉**，否則被擋的單還會佔用曝險額度。
    """

    def __init__(
        self,
        ledger: PositionAttributionLedger,
        dao: Optional[LiveTradeDAO] = None,
        run_id: str = "",
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立守門
        - Parameters:
            - ledger: PositionAttributionLedger
                部位歸屬帳
            - dao: Optional[LiveTradeDAO]
                紀錄庫；被擋的單寫進 `live_risk_event`
            - run_id: str
                本次啟動的識別碼
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        self.ledger: PositionAttributionLedger = ledger
        self.dao: Optional[LiveTradeDAO] = dao
        self.run_id: str = run_id
        self._now: Callable[[], datetime.datetime] = now_provider

    def filter(
        self,
        orders: Sequence[Tuple[str, BaseOrder]],
        pending_holders: Optional[Dict[str, str]] = None,
    ) -> List[Tuple[str, BaseOrder]]:
        """
        - Description:
            擋下與他人衝突的新倉單；**平倉單一律放行**

            擋平倉會讓部位失去出場能力，那比衝突嚴重得多。

            輸入順序即優先順序（呼叫端已依 `sort_orders()` 排好）——
            **不要用策略的註冊順序**，那會讓結果取決於設定檔的排列。
        - Parameters:
            - orders: Sequence[Tuple[str, BaseOrder]]
                `[(策略名, 訂單)]`，順序即優先順序
            - pending_holders: Optional[Dict[str, str]]
                目前有未終結委託的標的 `{symbol: 策略}`；掛單中的標的同樣算被佔住
        - Return:
            - List[Tuple[str, BaseOrder]]
                放行的委託
        """

        holders: Dict[str, str] = self._collect_holders(orders, pending_holders)
        closing: Dict[int, bool] = {
            index: self._is_closing(order) for index, (_, order) in enumerate(orders)
        }

        allowed, blocked = resolve_symbol_conflicts(orders, holders, closing)

        for strategy_name, order, reason in blocked:
            logger.warning(reason)
            self._write_event(reason, strategy_name, order.symbol)

        return allowed

    def _collect_holders(
        self,
        orders: Sequence[Tuple[str, BaseOrder]],
        pending_holders: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        """
        查出本批涉及的標的目前由誰持有

        **只查本批用得到的標的**：整張 lot 表掃一遍在標的池大時會很慢，
        而守門跑在尾盤那 4 分鐘裡。
        """

        holders: Dict[str, str] = {}
        for _, order in orders:
            if order.symbol in holders:
                continue
            holder: Optional[str] = self.ledger.get_holder(order.symbol)
            if holder is not None:
                holders[order.symbol] = holder

        # 掛單中的標的同樣算被佔住：等它成交才發現衝突就來不及了
        for symbol, strategy_name in (pending_holders or {}).items():
            holders.setdefault(symbol, strategy_name)

        return holders

    @staticmethod
    def _is_closing(order: BaseOrder) -> bool:
        """
        是否為平倉單

        判定委派給共用的動作推導，**不在這裡自己寫一份** if——
        散在多處會漂移，而漂移的後果是平倉單被當成新倉擋掉。
        """

        return order.action is resolve_close_action(order.position_type)

    def _write_event(self, message: str, strategy_name: str, symbol: str) -> None:
        """寫一筆風控事件；類別固定為 parity 認得的那一個"""

        if self.dao is None:
            return

        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": strategy_name,
                "severity": "WARN",
                "category": CROSS_STRATEGY_BLOCKED,
                "symbol": symbol,
                "message": message,
                "occurred_at": self._now(),
            }
        )
