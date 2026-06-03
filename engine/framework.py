"""Three industry-chain frameworks + 6-month momentum tag.

F1 · 下游传导   downstream lag thesis (12-18mo money flow into power/SMR/...)
F2 · 财报真空   earnings vacuum reversal (long base + still healthy)
F3 · 新节点     new architecture position (凭空多出的位置)
6M-MOM          6-month avg monthly return ≥ 3% (clean LT momentum)
"""
from __future__ import annotations

# F3 — explicit "new node" whitelist
NEW_NODE_TICKERS = {
    "SNDK": "NAND 独立后凭空多出的纯 NAND 龙头",
    "ALAB": "GPU 机柜密度催生的 PCIe Retimer 新位置",
    "AAOI": "800G 光模块爬坡过程的新供应商",
    "CRDO": "AEC 有源铜缆 — 替代部分光模块的新位置",
    "CRWV": "Neocloud — hyperscaler 之外的 GPU 出租新位置",
    "IREN": "矿改算力 — 利用闲置电力的新位置",
    "MTSI": "TIA/Driver — 800G 光模块带动的新增长点",
    "GLW":  "CPO 玻璃光纤的新需求位置",
    "CAMT": "HBM 检测设备 — 凭空多出的设备位置",
    "TTMI": "AI 高端 PCB — 复杂度跳级的新位置",
    "AXTI": "InP 衬底卡脖子 — 光模块上游的关键位置",
    "POET": "硅光集成 — CPO 时代的新平台",
    "BE":   "燃料电池 — IDC 现场发电的新位置",
    "OKLO": "微堆 — SMR 之外的新核电形态",
    "NBIS": "欧洲 Neocloud — 区域 GPU 出租新位置",
    "APLD": "HPC 托管 IDC — 矿改资源的新位置",
}

# F1 — downstream layers (12-18 mo lag, money flows here LATE)
DOWNSTREAM_LAYER_BONUS = {
    "L10": 5,  # 电力配套
    "L11": 5,  # 核电/SMR
    "L12": 4,  # 水电/可再生
    "L9":  3,  # 半导体底座 (设备+材料)
    "L6":  3,  # 光通信 (持续传导期)
    "L13": 3,  # 燃料电池/储能
}

FRAMEWORK_META = {
    "F1": {"emoji": "\U0001f30a", "label": "下游传导", "color": "#3498db", "desc": "12-18 月资金传导期"},
    "F2": {"emoji": "\U0001f9ee", "label": "财报真空", "color": "#9b59b6", "desc": "长底+健康，黎明前"},
    "F3": {"emoji": "\U0001f4a1", "label": "新节点",   "color": "#e67e22", "desc": "凭空多出来的位置"},
    "6M": {"emoji": "\U0001f4c5", "label": "6月均值",  "color": "#16a085", "desc": "6月月均≥3%"},
}


def layer_key(u):
    return (u.get("layer") or "").split("·")[0]


def downstream_bonus(u):
    return DOWNSTREAM_LAYER_BONUS.get(layer_key(u), 0)


def new_node_bonus(u):
    return 5 if u.get("ticker") in NEW_NODE_TICKERS else 0


def detect_framework_tags(u, p, comp_score, conviction):
    """Return list of {"code", "reason"} for which frameworks this stock fits."""
    tags = []
    t = u.get("ticker")
    lk = layer_key(u)

    # F1 · downstream lag
    if lk in ("L10", "L11", "L12") and conviction >= 50:
        tags.append({"code": "F1", "reason": f"{u.get('layer')} 在 12-18 月传导期, 综合分 {comp_score:.0f}"})

    # F3 · new node (whitelist)
    if t in NEW_NODE_TICKERS:
        tags.append({"code": "F3", "reason": NEW_NODE_TICKERS[t]})

    # F2 · earnings vacuum reversal
    d52 = p.get("dist_52w_high_pct") or 0
    if (-30 <= d52 <= -10
            and p.get("above_sma_50")
            and comp_score >= 70):
        tags.append({"code": "F2", "reason": f"距 52W 高 {d52:.0f}%, 在 SMA50 上, 综合分 {comp_score:.0f}"})

    # 6M-Mom · clean long-term momentum
    avg6m = p.get("avg_monthly_6m_pct")
    if avg6m is not None and avg6m >= 3:
        tags.append({"code": "6M", "reason": f"6 月月均 {avg6m:.1f}%/月"})

    return tags
