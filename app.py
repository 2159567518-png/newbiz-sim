# app.py
import random
import math
import uuid
import json
import time
from threading import Lock, Thread

from flask import Flask, render_template, request, send_file, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

# ========== 配置 ==========
RULES_DOC_PATH = "/mnt/data/好.docx"   # <--- 你上传的文件路径（助手已为你预填）
INITIAL_CAPITAL = 10_000_000

# socketio config
async_mode = "eventlet"  # eventlet 安装后使用
app = Flask(__name__)
app.config['SECRET_KEY'] = "newbiz_secret_key"
socketio = SocketIO(app, async_mode=async_mode, cors_allowed_origins="*")
thread_lock = Lock()

# ========== 基本数据模型（简化并适配多人） ==========
MATERIALS = ["木材", "金属", "布料", "塑料"]
AI_MARKETS_DEF = [
    {"name": "市场A", "wealth": 1.1, "population": 1.2},
    {"name": "市场B", "wealth": 0.9, "population": 1.0},
    {"name": "市场C", "wealth": 1.2, "population": 0.8},
    {"name": "市场D", "wealth": 1.0, "population": 1.1}
]

PRODUCTION_COST_PER_ITEM = 1000
RAW_BASE_EXTRACTION_COST = 500
MINER_BASE_COST = 100_000
PROD_LINE_COST = 200_000

# ========== 全局游戏状态（内存） ==========
GAME = {
    "started": False,
    "config": {
        "companies": 6,
        "countries": 8,
        "years": 4,
        "trial_year_index": 1,
        "initial_capital": INITIAL_CAPITAL
    },
    "year": 0,
    "is_trial": False,
    "companies": {},   # company_id -> company dict
    "market_listings": {m: [] for m in MATERIALS},  # simple listings
    "ai_markets": [],
    "countries": [],
    "government": {"tax_rate": 0.2},
    "history": []
}

# ========== 工具函数 ==========
def gen_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"

def now_ts():
    return int(time.time())

# ========== 初始化玩法元素 ==========
def init_game(config=None):
    cfg = GAME["config"]
    if config:
        cfg.update(config)
    GAME["year"] = 0
    GAME["started"] = True
    GAME["ai_markets"] = []
    for d in AI_MARKETS_DEF:
        # give each market a random material preference
        pref = random.choice(MATERIALS)
        GAME["ai_markets"].append({"name": d["name"], "wealth": d["wealth"], "population": d["population"], "preference": pref})

    # initialize countries
    GAME["countries"] = []
    for i in range(cfg["countries"]):
        resource = MATERIALS[i % len(MATERIALS)]
        GAME["countries"].append({"name": f"国{i+1}", "resource": resource, "cycle": "normal", "mult": 1.0})

    # create companies (unassigned players can claim)
    GAME["companies"] = {}
    for i in range(cfg["companies"]):
        cid = f"公司{i+1}"
        company = {
            "id": cid,
            "name": cid,
            "country": GAME["countries"][i % len(GAME["countries"])]["name"],
            "cash": cfg["initial_capital"],
            "inventory_products": 0,
            "inventory_raw": {m: 0 for m in MATERIALS},
            "miners": [],
            "production_lines": [],
            "rnd_spent": 0,
            "brand_value": 0.0,
            "product_base_value": 2500.0,
            "product_tier": "low",
            "product_raws": random.sample(MATERIALS, 2),
            "loans": [],
            "system_orders": [],
            "owner": None,   # socket session id or player name who claimed it
            "total_assets": cfg["initial_capital"]
        }
        # seed a miner & prod line for convenience
        buy_miner(company)
        buy_production_line(company)
        GAME["companies"][cid] = company

    # clear market listings
    GAME["market_listings"] = {m: [] for m in MATERIALS}
    GAME["history"] = []

def buy_miner(company, cost=MINER_BASE_COST, output=1):
    miner = {"id": gen_id("miner"), "cost": cost, "output": output, "age": 0}
    company["miners"].append(miner)
    company["cash"] -= cost
    return miner

def buy_production_line(company, cost=PROD_LINE_COST, capacity=200):
    pl = {"id": gen_id("pline"), "cost": cost, "capacity": capacity, "age": 0, "can_produce_tier": ["low", "mid"]}
    company["production_lines"].append(pl)
    company["cash"] -= cost
    return pl

# ========== 市场接口 ==========
def post_listing(company_id, material, qty, price):
    order = {"id": gen_id("list"), "seller": company_id, "qty": qty, "price": price, "ts": now_ts()}
    GAME["market_listings"][material].append(order)
    return order

def match_buy(buyer_id, material, qty, max_unit_price):
    # match cheapest first
    listings = sorted(GAME["market_listings"][material], key=lambda x: x["price"])
    acquired = []
    remaining = qty
    to_remove = []
    for l in listings:
        if remaining <= 0: break
        if l["price"] <= max_unit_price:
            take = min(remaining, l["qty"])
            acquired.append({"seller": l["seller"], "qty": take, "price": l["price"], "order_id": l["id"]})
            l["qty"] -= take
            remaining -= take
            if l["qty"] <= 0:
                to_remove.append(l)
    for r in to_remove:
        GAME["market_listings"][material].remove(r)
    return acquired

def estimate_unit_price(material):
    base = 1000
    var = (random.random() - 0.5) * 0.2
    return max(1, int(base * (1 + var)))

# ========== 公司方法（简单封装） ==========
def company_asset_value(comp):
    # fixed asset value with simple age-based depreciation
    value = 0
    for a in comp["miners"] + comp["production_lines"]:
        age = a.get("age", 0)
        base = a.get("cost", 0)
        effective = base * max(0.2, (1 - 0.2 * age))
        value += effective
    inventory_val = comp["inventory_products"] * PRODUCTION_COST_PER_ITEM + sum(comp["inventory_raw"].values()) * RAW_BASE_EXTRACTION_COST
    comp["total_assets"] = comp["cash"] + value + inventory_val
    return comp["total_assets"]

def mine_raw(comp, material, qty):
    total_output = sum(m.get("output", 0) for m in comp["miners"])
    if total_output <= 0:
        return 0, "no_miner"
    possible = min(qty, int(total_output))
    unit_cost = RAW_BASE_EXTRACTION_COST
    cost = possible * unit_cost
    if comp["cash"] < cost:
        possible = int(comp["cash"] / unit_cost)
        cost = possible * unit_cost
    comp["cash"] -= cost
    comp["inventory_raw"][material] += possible
    return possible, cost

def produce_products(comp, qty):
    total_capacity = sum(pl.get("capacity", 0) for pl in comp["production_lines"])
    if total_capacity <= 0:
        return 0, "no_production_line"
    can_by_raw = min(comp["inventory_raw"].get(r, 0) for r in comp["product_raws"])
    actual = min(qty, total_capacity, can_by_raw)
    cost = int(actual * PRODUCTION_COST_PER_ITEM)
    if comp["cash"] < cost:
        actual = int(comp["cash"] / PRODUCTION_COST_PER_ITEM)
        cost = actual * PRODUCTION_COST_PER_ITEM
    for r in comp["product_raws"]:
        comp["inventory_raw"][r] -= actual
        if comp["inventory_raw"][r] < 0:
            comp["inventory_raw"][r] = 0
    comp["cash"] -= cost
    comp["inventory_products"] += actual
    return actual, cost

def sell_to_market(comp, market, price):
    if comp["inventory_products"] <= 0:
        return 0, 0
    material_bonus = 1.2 if market["preference"] in comp["product_raws"] else 1.0
    customer_value = comp["product_base_value"] * market["wealth"] * market["population"] * material_bonus * (1 + comp["brand_value"] / 200.0)
    if price <= customer_value:
        qty_sold = comp["inventory_products"]
    else:
        ratio = customer_value / price
        qty_sold = int(comp["inventory_products"] * ratio)
    revenue = qty_sold * price
    comp["inventory_products"] -= qty_sold
    comp["cash"] += revenue
    comp["brand_value"] += qty_sold * 0.001
    return qty_sold, revenue

def year_end_settle(comp):
    # loans
    for loan in list(comp["loans"]):
        if loan.get("remaining_years", 0) <= 1:
            repayment = int(loan["amount"] * (1 + loan["rate"]))
            comp["cash"] = max(0, comp["cash"] - repayment)
            comp["loans"].remove(loan)
        else:
            loan["remaining_years"] = loan.get("remaining_years", 1) - 1
    profit_indicator = max(0, comp["cash"] - GAME["config"]["initial_capital"])
    tax = int(profit_indicator * GAME["government"].get("tax_rate", 0.2))
    comp["cash"] = max(0, comp["cash"] - tax)
    ware_rate = {"low": 50, "mid": 100, "high": 150}
    ware_fee = int(comp["inventory_products"] * ware_rate.get(comp["product_tier"], 50))
    comp["cash"] = max(0, comp["cash"] - ware_fee)
    for a in comp["miners"] + comp["production_lines"]:
        a["age"] = a.get("age", 0) + 1
    company_asset_value(comp)
    return {"tax_paid": tax, "ware_fee": ware_fee, "assets": comp["total_assets"]}

# ========== 游戏循环：逐年执行简化逻辑 ==========
def advance_year():
    cfg = GAME["config"]
    GAME["year"] += 1
    year = GAME["year"]
    is_trial = (year == cfg["trial_year_index"])
    GAME["is_trial"] = is_trial

    # government events (simplified)
    p = random.random()
    events = []
    if p < 0.15:
        # subsidy
        total = random.randint(200_000, 800_000)
        c = random.choice(GAME["countries"])
        events.append({"type": "subsidy", "country": c["name"], "amount": total})
        # give subsidy to all companies in that country
        for comp in GAME["companies"].values():
            if comp["country"] == c["name"]:
                comp["cash"] += total // 3
    elif p < 0.28:
        mat = random.choice(MATERIALS)
        qty = random.randint(100, 800)
        price = estimate_unit_price(mat) * (1 + random.uniform(0.05, 0.25))
        events.append({"type": "gov_order", "material": mat, "qty": qty, "price": int(price)})

    GAME["history"].append({"year": year, "events": events})

    # each company performs simple auto actions (for demonstration)
    for cid, comp in GAME["companies"].items():
        # if owner exists and is "manual", skip automatic actions -- server auto actions for AI companies only
        if comp.get("owner") is None:
            # basic AI strategy:
            # buy some raw if inventory raw less than target
            for r in comp["product_raws"]:
                if comp["inventory_raw"].get(r, 0) < 300:
                    est = estimate_unit_price(r)
                    acquired = match_buy(cid, r, 200, int(est * 1.2))
                    # perform transfers
                    if acquired:
                        total_got = 0; total_spent = 0
                        for a in acquired:
                            seller = GAME["companies"][a["seller"]]
                            amount = a["qty"]
                            price = a["price"]
                            total_got += amount
                            total_spent += amount * price
                            seller["cash"] += amount * price
                        comp["inventory_raw"][r] += total_got
                        comp["cash"] -= total_spent
            # produce a batch
            produced, cost = produce_products(comp, 200)
            # sell to a random market node
            if comp["inventory_products"] > 0:
                market = random.choice(GAME["ai_markets"])
                price = int(comp["product_base_value"] * (1 + random.choice([0.5, 0.2, 0.0])))
                qty, rev = sell_to_market(comp, market, price)
    # year-end settlement
    for comp in GAME["companies"].values():
        year_end_settle(comp)

    # trial year cleanup
    if is_trial:
        for comp in GAME["companies"].values():
            comp["cash"] = cfg["initial_capital"]
            comp["inventory_products"] = 0
            comp["inventory_raw"] = {m: 0 for m in MATERIALS}
            comp["miners"] = []
            comp["production_lines"] = []
            comp["loans"] = []
            comp["rnd_spent"] = 0
            company_asset_value(comp)

    # compute country cycle (from year 2)
    if year >= 2:
        assets_by_country = {}
        for comp in GAME["companies"].values():
            assets_by_country.setdefault(comp["country"], []).append(comp["total_assets"])
        for c in GAME["countries"]:
            arr = assets_by_country.get(c["name"], [])
            avg = (sum(arr) / len(arr)) if arr else cfg["initial_capital"]
            if avg > 1.5 * cfg["initial_capital"]:
                cycle = "overheat"; mult = 1.25
            elif avg > 1.1 * cfg["initial_capital"]:
                cycle = "boom"; mult = 1.1
            elif avg < 0.8 * cfg["initial_capital"]:
                cycle = "recession"; mult = 0.9
            else:
                cycle = "normal"; mult = 1.0
            c["cycle"] = cycle; c["mult"] = mult

# ========== Socket IO handlers ==========
@socketio.on('connect')
def handle_connect():
    sid = request.sid
    emit('game_state', compact_game_state(), room=sid)
    print("Client connected:", sid)

@socketio.on('claim_company')
def handle_claim(data):
    """
    data: { company_id: "公司1", player_name: "alice" }
    """
    sid = request.sid
    cid = data.get("company_id")
    player = data.get("player_name") or sid
    comp = GAME["companies"].get(cid)
    if not comp:
        emit('claim_result', {'ok': False, 'msg': '公司不存在'})
        return
    if comp.get("owner") and comp["owner"] != sid:
        emit('claim_result', {'ok': False, 'msg': f'公司已被占用：{comp["owner"]}'})
        return
    comp["owner"] = sid
    comp["player_name"] = player
    emit('claim_result', {'ok': True, 'company': comp}, room=sid)
    socketio.emit('game_state', compact_game_state())

@socketio.on('release_company')
def handle_release(data):
    sid = request.sid
    cid = data.get("company_id")
    comp = GAME["companies"].get(cid)
    if comp and comp.get("owner") == sid:
        comp["owner"] = None
        comp.pop("player_name", None)
    socketio.emit('game_state', compact_game_state())

@socketio.on('player_action')
def handle_player_action(data):
    """
    data example:
    { action: "buy_miner" | "buy_prod_line" | "mine" | "produce" | "post_listing" | "buy_market" | "sell_to_market" | "take_loan",
      company_id: "公司1", params: {...} }
    """
    sid = request.sid
    action = data.get("action")
    cid = data.get("company_id")
    comp = GAME["companies"].get(cid)
    if not comp:
        emit('action_result', {'ok': False, 'msg': '公司不存在'}, room=sid); return
    if comp.get("owner") != sid:
        emit('action_result', {'ok': False, 'msg': '你未拥有该公司'}, room=sid); return

    result = {'ok': False, 'action': action}
    try:
        if action == "buy_miner":
            m = buy_miner(comp)
            result = {'ok': True, 'msg': '买入矿机', 'miner': m, 'cash': comp['cash']}
        elif action == "buy_prod_line":
            pl = buy_production_line(comp)
            result = {'ok': True, 'msg': '买入生产线', 'prod_line': pl, 'cash': comp['cash']}
        elif action == "mine":
            mat = data['params'].get('material')
            qty = int(data['params'].get('qty', 0))
            q, cost = mine_raw(comp, mat, qty)
            result = {'ok': True, 'msg': f'开采 {q} {mat} 成本 {cost}', 'cash': comp['cash']}
        elif action == "produce":
            qty = int(data['params'].get('qty', 0))
            q, cost = produce_products(comp, qty)
            result = {'ok': True, 'msg': f'生产 {q} 件 成本 {cost}', 'cash': comp['cash'], 'inventory_products': comp['inventory_products']}
        elif action == "post_listing":
            mat = data['params'].get('material'); qty = int(data['params'].get('qty', 0)); price = int(data['params'].get('price', 0))
            order = post_listing(cid, mat, qty, price)
            comp['system_orders'].append(order)
            result = {'ok': True, 'msg': '已上架', 'order': order}
        elif action == "buy_market":
            mat = data['params'].get('material'); qty = int(data['params'].get('qty', 0)); maxp = int(data['params'].get('max_price', 1000000))
            acquired = match_buy(cid, mat, qty, maxp)
            total_got = 0; total_spent = 0
            for a in acquired:
                seller = GAME["companies"][a["seller"]]
                amount = a["qty"]; price = a["price"]
                total_got += amount; total_spent += amount * price
                seller["cash"] += amount * price
            comp["cash"] -= total_spent
            comp["inventory_raw"][mat] += total_got
            result = {'ok': True, 'got': total_got, 'spent': total_spent, 'cash': comp['cash']}
        elif action == "sell_to_market":
            price = int(data['params'].get('price', 0))
            market = random.choice(GAME["ai_markets"])
            qty, rev = sell_to_market(comp, market, price)
            result = {'ok': True, 'sold': qty, 'revenue': rev, 'cash': comp['cash']}
        elif action == "take_loan":
            amt = int(data['params'].get('amount', 0)); term = int(data['params'].get('term', 1)); rate = float(data['params'].get('rate', 0.12))
            loan = {"id": gen_id("loan"), "amount": amt, "term": term, "rate": rate, "remaining_years": term}
            comp["loans"].append(loan); comp['cash'] += amt
            result = {'ok': True, 'msg': '贷款成功', 'loan': loan, 'cash': comp['cash']}
        else:
            result = {'ok': False, 'msg': '未知动作'}
    except Exception as e:
        result = {'ok': False, 'msg': str(e)}

    # broadcast updated state to all
    socketio.emit('action_result', result, room=sid)
    socketio.emit('game_state', compact_game_state())

@socketio.on('advance_year')
def handle_advance_year(data):
    # only allow if server permits (could add owner/admin check)
    advance_year()
    socketio.emit('game_state', compact_game_state())
    emit('advance_result', {'ok': True, 'year': GAME['year']})

@socketio.on('get_rules')
def handle_get_rules():
    # send path (front-end can request it)
    emit('rules_path', {'path': RULES_DOC_PATH})

# ========== helper to send compact state ==========
def compact_game_state():
    # include companies summary, market listings, ai_markets, year, countries
    comp_summaries = {}
    for cid, c in GAME["companies"].items():
        comp_summaries[cid] = {
            "id": c["id"],
            "name": c["name"],
            "country": c["country"],
            "cash": c["cash"],
            "inventory_products": c["inventory_products"],
            "inventory_raw": c["inventory_raw"],
            "miners": c["miners"],
            "production_lines": c["production_lines"],
            "brand_value": c["brand_value"],
            "owner": c.get("player_name") or c.get("owner"),
            "product_base_value": c["product_base_value"],
            "product_raws": c["product_raws"],
            "total_assets": c.get("total_assets", 0)
        }
    state = {
        "year": GAME["year"],
        "is_trial": GAME["is_trial"],
        "companies": comp_summaries,
        "market_listings": GAME["market_listings"],
        "ai_markets": GAME["ai_markets"],
        "countries": GAME["countries"],
        "government": GAME["government"],
        "config": GAME["config"]
    }
    return state

# ========== HTTP routes ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/rules.docx')
def rules_doc():
    # serve the uploaded docx file directly from local path
    try:
        return send_file(RULES_DOC_PATH, as_attachment=False)
    except Exception as e:
        return f"Rules file not found at {RULES_DOC_PATH}: {e}", 404

@app.route('/start', methods=['POST'])
def http_start():
    cfg = request.json or {}
    init_game(cfg)
    return jsonify({"ok": True, "msg": "game initialized", "config": GAME["config"]})

@app.route('/state')
def http_state():
    return jsonify(compact_game_state())

# ========== run server ==========
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000)

