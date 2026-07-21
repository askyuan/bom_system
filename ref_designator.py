"""
位号（Reference Designator）解析与智能合并模块

核心能力：
- 解析逗号/空格分隔的位号字符串
- 按前缀分组、数字排序
- 连续数字合并为范围显示（C1,C2,C3 → C1-C3）
- 超长字符串截断策略
"""

import re
from typing import List, Tuple


# 位号解析正则：前缀字母 + 数字
_REF_PATTERN = re.compile(r'^([A-Za-z]+)(\d+)$')


def parse_designators(raw: str) -> List[Tuple[str, int]]:
    """
    解析位号字符串，返回 [(prefix, number), ...] 列表。
    支持逗号、空格、分号分隔，自动去重。
    """
    if not raw:
        return []

    # 统一分隔符
    normalized = raw.replace(';', ',').replace(' ', ',')
    tokens = [t.strip() for t in normalized.split(',') if t.strip()]

    result = []
    seen = set()
    for token in tokens:
        # 尝试展开范围标记（如 "C1-C3"）
        range_match = re.match(r'^([A-Za-z]+)(\d+)\s*[-–]\s*\1?(\d+)$', token)
        if range_match:
            prefix = range_match.group(1)
            start, end = int(range_match.group(2)), int(range_match.group(3))
            for n in range(min(start, end), max(start, end) + 1):
                key = (prefix.upper(), n)
                if key not in seen:
                    seen.add(key)
                    result.append(key)
        else:
            m = _REF_PATTERN.match(token)
            if m:
                key = (m.group(1).upper(), int(m.group(2)))
                if key not in seen:
                    seen.add(key)
                    result.append(key)

    return result


def count_designators(raw: str) -> int:
    """返回位号数量。"""
    return len(parse_designators(raw))


def merge_designators(
    designators: List[Tuple[str, int]],
    max_display_len: int = 120,
) -> str:
    """
    智能合并位号列表为紧凑显示字符串。

    规则：
    1. 按前缀字母分组
    2. 同组内按数字升序排列
    3. 连续数字（步长为1）合并为 "前缀起始-前缀末尾"
    4. 非连续用逗号分隔
    5. 超长截断并追加 "...共N个"

    示例：
    [(C,1),(C,2),(C,3),(C,5),(C,8),(C,9),(R,1),(R,2)]
    → "C1-C3,C5,C8-C9,R1-R2"
    """
    if not designators:
        return ""

    # 按前缀分组
    groups: dict = {}
    for prefix, num in designators:
        groups.setdefault(prefix, []).append(num)

    # 各组内排序并合并
    parts = []
    for prefix in sorted(groups.keys()):
        nums = sorted(set(groups[prefix]))
        ranges = _merge_to_ranges(nums)
        for rng in ranges:
            if rng[0] == rng[1]:
                parts.append(f"{prefix}{rng[0]}")
            else:
                parts.append(f"{prefix}{rng[0]}-{prefix}{rng[1]}")

    result = ",".join(parts)

    # 截断策略
    total_count = len(designators)
    if len(result) > max_display_len:
        # 找到截断位置
        truncated = result[:max_display_len]
        # 尝试在逗号处截断
        last_comma = truncated.rfind(',')
        if last_comma > max_display_len // 2:
            truncated = truncated[:last_comma]
        result = f"{truncated},...共{total_count}个"

    return result


def _merge_to_ranges(nums: List[int]) -> List[Tuple[int, int]]:
    """将有序数字列表合并为连续范围列表。"""
    if not nums:
        return []

    ranges = []
    start = nums[0]
    end = nums[0]

    for n in nums[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append((start, end))
            start = n
            end = n

    ranges.append((start, end))
    return ranges


def format_designators(raw: str, max_display_len: int = 120) -> str:
    """
    一步完成：解析原始位号字符串 → 智能合并显示。
    """
    parsed = parse_designators(raw)
    return merge_designators(parsed, max_display_len)


def expand_range(range_str: str) -> List[str]:
    """
    展开位号范围字符串为完整列表。
    例如 "C1-C3,R5" → ["C1","C2","C3","R5"]
    """
    parsed = parse_designators(range_str)
    return [f"{prefix}{num}" for prefix, num in parsed]
