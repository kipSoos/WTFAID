from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime
from central_bank_engine import CentralBankGame, Decision

app = Flask(__name__, static_folder='.', template_folder='.')
CORS(app)  # Cho phép gọi API từ frontend

# Biến lưu trữ game instance toàn cục cho phiên chơi hiện tại
game_instance = None

@app.route('/')
def index():
    """Phục vụ file index.html khi truy cập trang chủ"""
    return send_from_directory('.', 'index.html')

@app.route('/api/start-game', methods=['POST'])
def start_game():
    """API khởi tạo trò chơi mới"""
    global game_instance
    data = request.json or {}
    start_date_str = data.get('start_date', '02/02/2025')
    initial_rate = float(data.get('initial_rate', 3.95))
    
    try:
        start_date = datetime.strptime(start_date_str, "%d/%m/%Y").date()
        # Khởi tạo engine Python gốc
        game_instance = CentralBankGame(start_date, initial_rate, floor=0.5, cap=5.0)
        tb = game_instance.initialize_market_tbill(face_value=10000)
        
        return jsonify({
            "status": "success",
            "message": "Game initialized successfully via Python backend",
            "current_date": game_instance.current_date.strftime("%d/%m/%Y"),
            "initial_interbank_rate": game_instance.interbank_rate,
	    "tbill_inventory": game_instance.tbill_inventory(),
            "repo_inventory": game_instance.repo_inventory(),
            "initial_tbill": {
                "id": tb.security_id,
                "rate": tb.rate,
                "maturity_date": tb.maturity_date.strftime("%d/%m/%Y")
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/api/run-phase', methods=['POST'])
def run_phase():
    """API thực thi một Phase điều hành OMO dựa trên quyết định từ Frontend"""
    global game_instance
    if not game_instance:
        return jsonify({"status": "error", "message": "Game has not been started yet!"}), 400
        
    data = request.json or {}
    
    try:
        scenario_demand = float(data.get('scenario_demand', -1900))
        
        # Nhận dữ liệu và ánh xạ vào dataclass Decision của Python
        auction_method = data.get('auction_method')  # "Interest-rate auction" hoặc "Volume auction"
        omo_action = data.get('omo_action')          # "Repo", "Reverse Repo", "Buy Securities", "Sell Securities"
        volume = float(data.get('volume', 0))
        pricing_method = data.get('pricing_method')  # "Single-price", "Multi-price" hoặc None
        repo_rate = data.get('repo_rate')            # float hoặc None
        
        if repo_rate is not None:
            repo_rate = float(repo_rate)

        decision = Decision(
            auction_method=auction_method,
            omo_action=omo_action,
            volume=volume,
            pricing_method=pricing_method,
            repo_rate=repo_rate
        )
        
        # Gọi trực tiếp logic python gốc trong central_bank_engine.py
        result = game_instance.run_phase(scenario_demand, decision)
        
        # Chuyển đổi PhaseResult thành dạng JSON để trả về cho Frontend
        bids_list = [
            {
                "rank": b.rank,
                "bank": b.bank,
                "bid_rate": b.bid_rate,
                "settlement_rate": b.settlement_rate,
                "bid_volume": b.bid_volume,
                "real_volume": b.real_volume,
                "price": b.price,
                "won": b.won,
                "status": "Trúng thầu" if b.won else "Không trúng thầu"
            } for b in result.auction_bids
        ]
        
        response_data = {
            "phase": result.phase,
            "phase_date": result.phase_date.strftime('%d/%m/%Y'),
            "scenario_liquidity_demand": result.scenario_liquidity_demand,
            "unmet_from_previous_phase": result.unmet_from_previous_phase,
            "real_liquidity_demand": result.real_liquidity_demand,
            "supply": result.supply,
            "maturity_volume": result.maturity_volume,
            "total_supply": result.total_supply,
            "liquidity_gap": result.liquidity_gap,
            "liquidity_pressure": result.liquidity_pressure,
            "liquidity_adjusted_volume": result.liquidity_adjusted_volume,
            "previous_interbank_rate": result.previous_interbank_rate,
            "interbank_rate": result.interbank_rate,
            "total_bid_volume": result.total_bid_volume,
            "total_real_volume": result.total_real_volume,
            "win_rate": result.win_rate, # Dùng làm cut-off rate chuẩn từ Python
	    "tbill_inventory": game_instance.tbill_inventory(), # <-- Thêm dòng này
            "repo_inventory": game_instance.repo_inventory(),
            "bids": bids_list,
            "maturity_events": result.maturity_events
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == '__main__':
    print("Starting Central Bank OMO Flask Server on http://127.0.0.1:5000")
    app.run(debug=True, port=5000)