DEFAULT_THRESHOLD = 1.0


def classify(ch4_pct: float, threshold: float = DEFAULT_THRESHOLD) -> tuple[str, str]:
    if ch4_pct >= threshold:
        return "报警", f"甲烷达到报警线（{threshold:g}%）"
    return "正常", f"甲烷低于报警线（{threshold:g}%）"
