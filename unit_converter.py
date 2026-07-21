"""
单位转换引擎
负责解析电子元器件中常见的各种单位写法，统一转换为国际标准单位。
"""

import re
from typing import Optional, Tuple


class UnitConverter:
    """单位转换器：从数据库加载转换规则，支持电子元器件常见写法。"""

    def __init__(self, db_manager):
        self.db = db_manager
        self._rules: dict = {}       # alias -> (standard_unit, factor)
        self._category_rules: dict = {}  # category -> {alias -> (standard_unit, factor)}
        self._load_rules()

    def _load_rules(self):
        """从 unit_conversions 表加载转换规则。"""
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT alias, standard_unit, factor, category FROM unit_conversions"
            ).fetchall()
        for row in rows:
            alias, std_unit, factor, cat = row["alias"], row["standard_unit"], row["factor"], row["category"]
            entry = (std_unit, factor)
            if cat:
                self._category_rules.setdefault(cat, {})[alias.lower()] = entry
            else:
                self._rules[alias.lower()] = entry

    def reload(self):
        """重新加载规则（规则表更新后调用）。"""
        self._rules.clear()
        self._category_rules.clear()
        self._load_rules()

    def add_rule(self, alias: str, standard_unit: str, factor: float, category: Optional[str] = None):
        """添加一条转换规则到数据库并刷新缓存。"""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO unit_conversions (alias, standard_unit, factor, category) "
                "VALUES (?,?,?,?) ON CONFLICT(alias) DO UPDATE SET "
                "standard_unit=excluded.standard_unit, factor=excluded.factor, "
                "category=excluded.category",
                (alias, standard_unit, factor, category),
            )
        self.reload()

    def parse_value_unit(self, raw_str: str, category: Optional[str] = None) -> Optional[Tuple[float, str]]:
        """
        解析 "100nF"、"10K"、"4.7uF" 等字符串，返回 (标准化数值, 标准单位)。
        无法解析时返回 None。
        """
        if not raw_str or not isinstance(raw_str, str):
            return None

        raw_str = raw_str.strip()
        if not raw_str:
            return None

        # 尝试分离数值和单位部分
        match = re.match(
            r'^([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)\s*([a-zA-ZΩμ]+.*)$',
            raw_str
        )
        if not match:
            # 可能是纯数值（无单位）
            try:
                val = float(raw_str)
                return (val, "")
            except ValueError:
                return None

        num_str, unit_str = match.group(1), match.group(2).strip()
        try:
            value = float(num_str)
        except ValueError:
            return None

        # 查找匹配的单位规则
        result = self._lookup_unit(unit_str, category)
        if result:
            std_unit, factor = result
            return (value * factor, std_unit)

        # 未找到匹配：返回原始值，标记为待确认
        return (value, unit_str)

    def _lookup_unit(self, unit_str: str, category: Optional[str] = None) -> Optional[Tuple[str, float]]:
        """查找单位别名对应的标准单位和转换系数。"""
        key = unit_str.lower()

        # 先查分类专属规则
        if category and category in self._category_rules:
            if key in self._category_rules[category]:
                return self._category_rules[category][key]

        # 再查通用规则
        if key in self._rules:
            return self._rules[key]

        # 尝试去掉尾部 "ohm" 或 "Ω" 再查
        for suffix in ("ohm", "Ω"):
            if key.endswith(suffix):
                shortened = key[: -len(suffix)].rstrip()
                if shortened:
                    result = self._lookup_unit(shortened, category)
                    if result and result[0] == "Ohm":
                        return result

        return None

    def convert(self, value: float, from_unit: str, category: Optional[str] = None) -> Optional[Tuple[float, str]]:
        """将给定值和单位转换为标准单位。"""
        result = self._lookup_unit(from_unit, category)
        if result:
            std_unit, factor = result
            return (value * factor, std_unit)
        return (value, from_unit)

    def format_standard(self, value: float, unit: str) -> str:
        """将标准单位的值格式化为人类友好的字符串（如 0.0001F → 100uF）。"""
        prefixes = {
            "Ohm": [
                (1e6, "M"),
                (1e3, "K"),
                (1, ""),
            ],
            "F": [
                (1e-3, "mF"),
                (1e-6, "uF"),
                (1e-9, "nF"),
                (1e-12, "pF"),
            ],
            "H": [
                (1e-3, "mH"),
                (1e-6, "uH"),
                (1e-9, "nH"),
            ],
            "Hz": [
                (1e9, "GHz"),
                (1e6, "MHz"),
                (1e3, "kHz"),
                (1, "Hz"),
            ],
        }
        if unit in prefixes:
            for factor_val, prefix in prefixes[unit]:
                scaled = value / factor_val
                if 0.1 <= abs(scaled) < 1000:
                    if scaled == int(scaled):
                        return f"{int(scaled)}{prefix}{unit}"
                    return f"{scaled:g}{prefix}{unit}"
        # 回退
        if value == int(value):
            return f"{int(value)}{unit}"
        return f"{value:g}{unit}"


class FootprintNormalizer:
    """封装名称标准化器。"""

    def __init__(self, db_manager):
        self.db = db_manager
        self._aliases: dict = {}
        self._load_aliases()

    def _load_aliases(self):
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT alias, standard_name FROM footprint_aliases"
            ).fetchall()
        for row in rows:
            self._aliases[row["alias"].lower()] = row["standard_name"]

    def reload(self):
        self._aliases.clear()
        self._load_aliases()

    def normalize(self, footprint: Optional[str]) -> Optional[str]:
        """将封装名称标准化。无法识别时原样返回（转大写）。"""
        if not footprint:
            return None
        key = footprint.strip().lower()
        if key in self._aliases:
            return self._aliases[key]
        return footprint.strip().upper()

    def add_alias(self, alias: str, standard_name: str):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO footprint_aliases (alias, standard_name) VALUES (?,?) "
                "ON CONFLICT(alias) DO UPDATE SET standard_name=excluded.standard_name",
                (alias, standard_name),
            )
        self.reload()
