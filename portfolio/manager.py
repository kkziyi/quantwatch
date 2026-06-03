"""PortfolioManager — 组合管理核心

支持多组合持仓管理：
  - 组合 CRUD：创建 / 列表 / 查询 / 删除
  - 持仓 CRUD：添加 / 移除 / 更新 / 列表
  - JSON 持久化：portfolio/data/portfolio.json
  - CSV 批量导入
"""

import csv
import json
import os
from datetime import date
from pathlib import Path
from typing import Any


class PortfolioManager:
    """组合管理器 — 支持多组合、每只股票记录成本/持仓/买入日期。

    数据存储: JSON 文件 (默认 portfolio/data/portfolio.json)。
    每次增删改操作自动保存，重启后数据不丢失。
    """

    def __init__(self, data_file: str | None = None):
        """
        Args:
            data_file: JSON 数据文件路径。默认 portfolio/data/portfolio.json
                       （相对于本模块所在目录）。
        """
        if data_file is None:
            data_file = Path(__file__).parent / "data" / "portfolio.json"
        self._data_file = Path(data_file)
        self._data: dict[str, Any] = {"portfolios": []}
        self._load()

    # ══════════════════════════════════════════════════════════
    # 内部方法
    # ══════════════════════════════════════════════════════════

    def _load(self) -> None:
        """从 JSON 文件加载数据；文件不存在时使用空结构。"""
        if self._data_file.exists():
            with open(self._data_file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {"portfolios": []}
            self._save()

    def _save(self) -> None:
        """保存数据到 JSON 文件（自动创建目录）。"""
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._data_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2, default=str)

    def _find_portfolio(self, name: str) -> tuple[int, dict | None]:
        """查找组合，返回 (index, dict) 或 (-1, None)。"""
        for i, pf in enumerate(self._data["portfolios"]):
            if pf["name"] == name:
                return i, pf
        return -1, None

    def _find_position(self, portfolio: dict, code: str) -> tuple[int, dict | None]:
        """在指定组合中查找持仓，返回 (index, dict) 或 (-1, None)。"""
        for i, pos in enumerate(portfolio.get("positions", [])):
            if pos["code"] == code:
                return i, pos
        return -1, None

    # ══════════════════════════════════════════════════════════
    # 组合 CRUD
    # ══════════════════════════════════════════════════════════

    def create_portfolio(self, name: str, description: str = "") -> dict:
        """创建新组合。

        Args:
            name: 组合名称（不可重复）
            description: 组合描述

        Returns:
            创建成功的组合字典

        Raises:
            ValueError: 组合名已存在
        """
        idx, _ = self._find_portfolio(name)
        if idx >= 0:
            raise ValueError(f"组合 '{name}' 已存在，请使用其他名称")
        pf = {
            "name": name,
            "description": description,
            "created_at": date.today().isoformat(),
            "positions": [],
        }
        self._data["portfolios"].append(pf)
        self._save()
        return pf

    def list_portfolios(self) -> list[dict]:
        """列出所有组合（不含持仓明细，仅概览）。

        Returns:
            组合列表，每个元素含 name / description / created_at / position_count
        """
        return [
            {
                "name": pf["name"],
                "description": pf.get("description", ""),
                "created_at": pf.get("created_at", ""),
                "position_count": len(pf.get("positions", [])),
            }
            for pf in self._data["portfolios"]
        ]

    def list_portfolios_with_positions(self) -> list[dict]:
        """列出所有组合（含完整持仓明细）。

        一次读取返回所有数据，避免多次 get_portfolio() 调用。

        Returns:
            组合列表，每个元素含 name / description / created_at / positions
        """
        return self._data["portfolios"]

    def get_portfolio(self, name: str) -> dict:
        """查询组合完整信息（含持仓明细）。

        Args:
            name: 组合名称

        Returns:
            组合字典（含 positions 列表）

        Raises:
            ValueError: 组合不存在
        """
        _, pf = self._find_portfolio(name)
        if pf is None:
            raise ValueError(f"组合 '{name}' 不存在")
        return pf

    def delete_portfolio(self, name: str) -> bool:
        """删除组合。

        Args:
            name: 组合名称

        Returns:
            True 表示删除成功

        Raises:
            ValueError: 组合不存在
        """
        idx, _ = self._find_portfolio(name)
        if idx < 0:
            raise ValueError(f"组合 '{name}' 不存在")
        self._data["portfolios"].pop(idx)
        self._save()
        return True

    # ══════════════════════════════════════════════════════════
    # 持仓操作
    # ══════════════════════════════════════════════════════════

    def add_position(
        self,
        portfolio: str,
        code: str,
        name: str,
        cost_price: float,
        shares: int,
        buy_date: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        """向指定组合添加持仓。

        Args:
            portfolio: 组合名称
            code: 股票代码
            name: 股票名称
            cost_price: 成本价
            shares: 持仓数量（股）
            buy_date: 买入日期 "YYYY-MM-DD"
            stop_loss: 止损价（可选）
            take_profit: 止盈价（可选）

        Returns:
            添加后的持仓字典

        Raises:
            ValueError: 组合不存在 / 股票代码已存在于该组合
        """
        _, pf = self._find_portfolio(portfolio)
        if pf is None:
            raise ValueError(f"组合 '{portfolio}' 不存在")

        if "positions" not in pf:
            pf["positions"] = []

        idx, existing = self._find_position(pf, code)
        if idx >= 0:
            raise ValueError(f"股票 '{code}' 已存在于组合 '{portfolio}' 中")

        pos = {
            "code": code,
            "name": name,
            "cost_price": cost_price,
            "shares": shares,
            "buy_date": buy_date,
        }
        if stop_loss is not None:
            pos["stop_loss"] = stop_loss
        if take_profit is not None:
            pos["take_profit"] = take_profit

        pf["positions"].append(pos)
        self._save()
        return pos

    def remove_position(self, portfolio: str, code: str) -> bool:
        """从指定组合移除持仓。

        Args:
            portfolio: 组合名称
            code: 股票代码

        Returns:
            True 表示移除成功

        Raises:
            ValueError: 组合不存在 / 持仓不存在
        """
        _, pf = self._find_portfolio(portfolio)
        if pf is None:
            raise ValueError(f"组合 '{portfolio}' 不存在")

        idx, _ = self._find_position(pf, code)
        if idx < 0:
            raise ValueError(
                f"股票 '{code}' 不在组合 '{portfolio}' 中"
            )

        pf["positions"].pop(idx)
        self._save()
        return True

    def update_position(self, portfolio: str, code: str, **kwargs) -> dict:
        """更新持仓信息（部分更新）。

        Args:
            portfolio: 组合名称
            code: 股票代码
            **kwargs: 要更新的字段，支持: name, cost_price, shares,
                      buy_date, stop_loss, take_profit

        Returns:
            更新后的持仓字典

        Raises:
            ValueError: 组合不存在 / 持仓不存在
        """
        _, pf = self._find_portfolio(portfolio)
        if pf is None:
            raise ValueError(f"组合 '{portfolio}' 不存在")

        _, pos = self._find_position(pf, code)
        if pos is None:
            raise ValueError(
                f"股票 '{code}' 不在组合 '{portfolio}' 中"
            )

        allowed = {"name", "cost_price", "shares", "buy_date", "stop_loss", "take_profit"}
        for k, v in kwargs.items():
            if k in allowed:
                pos[k] = v

        self._save()
        return pos

    def list_positions(self, portfolio: str) -> list[dict]:
        """列出指定组合的所有持仓。

        Args:
            portfolio: 组合名称

        Returns:
            持仓列表

        Raises:
            ValueError: 组合不存在
        """
        pf = self.get_portfolio(portfolio)
        return pf.get("positions", [])

    # ══════════════════════════════════════════════════════════
    # 导入导出
    # ══════════════════════════════════════════════════════════

    def import_csv(self, portfolio: str, csv_path: str) -> int:
        """从 CSV 文件批量导入持仓。

        CSV 格式 (UTF-8, 含表头):
            code,name,cost_price,shares,buy_date,stop_loss,take_profit

        stop_loss 和 take_profit 列可选（空值表示不设置）。

        Args:
            portfolio: 目标组合名称
            csv_path: CSV 文件路径

        Returns:
            成功导入的条目数

        Raises:
            ValueError: 组合不存在
            FileNotFoundError: CSV 文件不存在
        """
        # 确保组合存在
        _, pf = self._find_portfolio(portfolio)
        if pf is None:
            raise ValueError(f"组合 '{portfolio}' 不存在")

        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"CSV 文件不存在: {csv_path}")

        count = 0
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row.get("code", "").strip()
                name = row.get("name", "").strip()
                try:
                    cost_price = float(row.get("cost_price", 0))
                except (ValueError, TypeError):
                    cost_price = 0.0
                try:
                    shares = int(float(row.get("shares", 0)))
                except (ValueError, TypeError):
                    shares = 0
                buy_date = row.get("buy_date", "").strip()

                if not code or not name:
                    continue  # 跳过空行

                # 检查是否已存在
                existing_idx, _ = self._find_position(pf, code)
                if existing_idx >= 0:
                    continue  # 跳过重复

                pos: dict[str, Any] = {
                    "code": code,
                    "name": name,
                    "cost_price": cost_price,
                    "shares": shares,
                    "buy_date": buy_date,
                }

                # 可选字段
                sl = row.get("stop_loss", "").strip()
                if sl:
                    try:
                        pos["stop_loss"] = float(sl)
                    except (ValueError, TypeError):
                        pass

                tp = row.get("take_profit", "").strip()
                if tp:
                    try:
                        pos["take_profit"] = float(tp)
                    except (ValueError, TypeError):
                        pass

                if "positions" not in pf:
                    pf["positions"] = []
                pf["positions"].append(pos)
                count += 1

        if count > 0:
            self._save()
        return count
