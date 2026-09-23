BASE_ALERT_PCT = 1.0


def classify(ch4_pct: float, alert_pct: float = BASE_ALERT_PCT) -> tuple[str, str]:
    if ch4_pct >= alert_pct:
        if alert_pct == BASE_ALERT_PCT:
            return "报警", "甲烷达到报警线"
        return "报警", f"甲烷达到临时报警线 {alert_pct:g}%"
    if alert_pct == BASE_ALERT_PCT:
        return "正常", "甲烷低于报警线"
    return "正常", f"甲烷低于临时报警线 {alert_pct:g}%（报警线 {BASE_ALERT_PCT:g}%）"
