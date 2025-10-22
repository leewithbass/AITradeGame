from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import time
import threading
from datetime import datetime

from trading_engine import TradingEngine
from market_data import MarketDataFetcher
from ai_trader import AITrader
from database import Database

app = Flask(__name__)
CORS(app)

db = Database('trading_bot.db')
market_fetcher = MarketDataFetcher()
trading_engines = {}
auto_trading_enabled = threading.Event()
auto_trading_enabled.set()
trading_thread = None


def build_ai_trader_from_model(model):
    return AITrader(
        api_key=model['api_key'],
        api_url=model['api_url'],
        model_name=model['model_name'],
        system_prompt=model.get('system_prompt') or '',
        user_prompt=model.get('user_prompt') or '',
        enable_cot=bool(model.get('enable_cot'))
    )


def create_trading_engine(model_id: int):
    model = db.get_model(model_id)
    if not model:
        return None
    
    engine = TradingEngine(
        model_id=model_id,
        db=db,
        market_fetcher=market_fetcher,
        ai_trader=build_ai_trader_from_model(model)
    )
    engine.last_run = None
    return engine

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/models', methods=['GET'])
def get_models():
    models = db.get_all_models()
    return jsonify(models)

@app.route('/api/models', methods=['POST'])
def add_model():
    data = request.json or {}
    
    try:
        initial_capital = float(data.get('initial_capital', 100000))
    except (TypeError, ValueError):
        initial_capital = 100000.0
    
    try:
        auto_run_interval = int(data.get('auto_run_interval', 180))
    except (TypeError, ValueError):
        auto_run_interval = 180
    if auto_run_interval <= 0:
        auto_run_interval = 180
    
    auto_run = bool(data.get('auto_run', True))
    enable_cot = bool(data.get('enable_cot', False))
    system_prompt = (data.get('system_prompt') or '').strip()
    user_prompt = (data.get('user_prompt') or '').strip()
    
    model_id = db.add_model(
        name=data['name'],
        api_key=data['api_key'],
        api_url=data['api_url'],
        model_name=data['model_name'],
        initial_capital=initial_capital,
        auto_run=auto_run,
        auto_run_interval=auto_run_interval,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        enable_cot=enable_cot
    )
    
    try:
        engine = create_trading_engine(model_id)
        if engine:
            trading_engines[model_id] = engine
            print(f"[INFO] Model {model_id} ({data['name']}) initialized")
    except Exception as e:
        print(f"[ERROR] Model {model_id} initialization failed: {e}")
    
    return jsonify({
        'id': model_id,
        'message': 'Model added successfully',
        'auto_run': auto_run,
        'auto_run_interval': auto_run_interval,
        'enable_cot': enable_cot
    })

@app.route('/api/models/<int:model_id>', methods=['DELETE'])
def delete_model(model_id):
    try:
        model = db.get_model(model_id)
        model_name = model['name'] if model else f"ID-{model_id}"
        
        db.delete_model(model_id)
        if model_id in trading_engines:
            del trading_engines[model_id]
        
        print(f"[INFO] Model {model_id} ({model_name}) deleted")
        return jsonify({'message': 'Model deleted successfully'})
    except Exception as e:
        print(f"[ERROR] Delete model {model_id} failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>/portfolio', methods=['GET'])
def get_portfolio(model_id):
    prices_data = market_fetcher.get_current_prices(['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE'])
    current_prices = {coin: prices_data[coin]['price'] for coin in prices_data}
    
    portfolio = db.get_portfolio(model_id, current_prices)
    account_value = db.get_account_value_history(model_id, limit=100)
    
    return jsonify({
        'portfolio': portfolio,
        'account_value_history': account_value
    })

@app.route('/api/models/<int:model_id>/trades', methods=['GET'])
def get_trades(model_id):
    limit = request.args.get('limit', 50, type=int)
    trades = db.get_trades(model_id, limit=limit)
    return jsonify(trades)

@app.route('/api/models/<int:model_id>/conversations', methods=['GET'])
def get_conversations(model_id):
    limit = request.args.get('limit', 20, type=int)
    conversations = db.get_conversations(model_id, limit=limit)
    return jsonify(conversations)

@app.route('/api/market/prices', methods=['GET'])
def get_market_prices():
    coins = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE']
    prices = market_fetcher.get_current_prices(coins)
    return jsonify(prices)

def sleep_with_auto_check(seconds: float):
    """Sleep in small steps so that auto trading pause takes effect quickly."""
    end_time = time.time() + seconds
    while time.time() < end_time and auto_trading_enabled.is_set():
        remaining = end_time - time.time()
        time.sleep(min(1.0, max(0.1, remaining)))


def ensure_trading_thread():
    """Start trading loop thread if not already running."""
    global trading_thread
    if trading_thread and trading_thread.is_alive():
        return
    trading_thread = threading.Thread(target=trading_loop, daemon=True)
    trading_thread.start()
    print("[INFO] Auto-trading thread started")


@app.route('/api/models/<int:model_id>/execute', methods=['POST'])
def execute_trading(model_id):
    model = db.get_model(model_id)
    if not model:
        return jsonify({'error': 'Model not found'}), 404
    
    if model_id not in trading_engines:
        engine = create_trading_engine(model_id)
        if not engine:
            return jsonify({'error': 'Failed to initialize trading engine'}), 500
        trading_engines[model_id] = engine
    else:
        trading_engines[model_id].ai_trader = build_ai_trader_from_model(model)
    
    try:
        engine = trading_engines[model_id]
        result = engine.execute_trading_cycle()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if model_id in trading_engines:
            trading_engines[model_id].last_run = time.time()

def trading_loop():
    print("[INFO] Trading loop started")
    
    while True:
        auto_trading_enabled.wait()
        if not auto_trading_enabled.is_set():
            continue
        try:
            if not trading_engines:
                sleep_with_auto_check(30)
                continue
            
            print(f"\n{'='*60}")
            print(f"[CYCLE] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"[INFO] Active models: {len(trading_engines)}")
            print(f"{'='*60}")
            
            active_intervals = []
            for model_id, engine in list(trading_engines.items()):
                model = db.get_model(model_id)
                if not model:
                    trading_engines.pop(model_id, None)
                    continue
                
                if not bool(model.get('auto_run')):
                    continue
                
                try:
                    interval = int(model.get('auto_run_interval', 180))
                except (TypeError, ValueError):
                    interval = 180
                if interval <= 0:
                    interval = 180
                active_intervals.append(interval)
                
                last_run = engine.last_run
                now = time.time()
                if last_run and (now - last_run) < interval:
                    continue
                
                try:
                    engine.ai_trader = build_ai_trader_from_model(model)
                    print(f"\n[EXEC] Model {model_id}")
                    result = engine.execute_trading_cycle()
                    
                    if result.get('success'):
                        print(f"[OK] Model {model_id} completed")
                        if result.get('executions'):
                            for exec_result in result['executions']:
                                signal = exec_result.get('signal', 'unknown')
                                coin = exec_result.get('coin', 'unknown')
                                msg = exec_result.get('message', '')
                                if signal != 'hold':
                                    print(f"  [TRADE] {coin}: {msg}")
                    else:
                        error = result.get('error', 'Unknown error')
                        print(f"[WARN] Model {model_id} failed: {error}")
                except Exception as e:
                    print(f"[ERROR] Model {model_id} exception: {e}")
                    import traceback
                    print(traceback.format_exc())
                finally:
                    engine.last_run = time.time()
            
            sleep_time = min(active_intervals) if active_intervals else 60
            if sleep_time < 5:
                sleep_time = 5
            
            print(f"\n{'='*60}")
            print(f"[SLEEP] Waiting {sleep_time} seconds for next cycle")
            print(f"{'='*60}\n")
            
            sleep_with_auto_check(sleep_time)
            
        except Exception as e:
            print(f"\n[CRITICAL] Trading loop error: {e}")
            import traceback
            print(traceback.format_exc())
            print("[RETRY] Retrying in 60 seconds\n")
            sleep_with_auto_check(60)
    
    print("[INFO] Trading loop stopped")


@app.route('/api/auto-trading', methods=['GET', 'POST'])
def auto_trading_control():
    if request.method == 'GET':
        return jsonify({'enabled': auto_trading_enabled.is_set()})
    
    data = request.json or {}
    enabled = bool(data.get('enabled'))
    
    if enabled:
        auto_trading_enabled.set()
        ensure_trading_thread()
        message = 'Auto trading enabled'
        print("[INFO] Auto trading resumed via API")
    else:
        auto_trading_enabled.clear()
        message = 'Auto trading paused'
        print("[INFO] Auto trading paused via API")
    
    return jsonify({'enabled': auto_trading_enabled.is_set(), 'message': message})

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    models = db.get_all_models()
    leaderboard = []
    
    prices_data = market_fetcher.get_current_prices(['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE'])
    current_prices = {coin: prices_data[coin]['price'] for coin in prices_data}
    
    for model in models:
        portfolio = db.get_portfolio(model['id'], current_prices)
        account_value = portfolio.get('total_value', model['initial_capital'])
        returns = ((account_value - model['initial_capital']) / model['initial_capital']) * 100
        
        leaderboard.append({
            'model_id': model['id'],
            'model_name': model['name'],
            'account_value': account_value,
            'returns': returns,
            'initial_capital': model['initial_capital']
        })
    
    leaderboard.sort(key=lambda x: x['returns'], reverse=True)
    return jsonify(leaderboard)

def init_trading_engines():
    try:
        models = db.get_all_models()
        
        if not models:
            print("[WARN] No trading models found")
            return
        
        print(f"\n[INIT] Initializing trading engines...")
        for model in models:
            model_id = model['id']
            model_name = model['name']
            
            try:
                engine = TradingEngine(
                    model_id=model_id,
                    db=db,
                    market_fetcher=market_fetcher,
                    ai_trader=build_ai_trader_from_model(model)
                )
                engine.last_run = None
                trading_engines[model_id] = engine
                print(f"  [OK] Model {model_id} ({model_name})")
            except Exception as e:
                print(f"  [ERROR] Model {model_id} ({model_name}): {e}")
                continue
        
        print(f"[INFO] Initialized {len(trading_engines)} engine(s)\n")
        
    except Exception as e:
        print(f"[ERROR] Init engines failed: {e}\n")

if __name__ == '__main__':
    db.init_db()
    
    print("\n" + "=" * 60)
    print("AI Trading Platform")
    print("=" * 60)
    
    init_trading_engines()
    
    if auto_trading_enabled.is_set():
        ensure_trading_thread()
        print("[INFO] Auto-trading enabled")
    
    print("\n" + "=" * 60)
    print("Server: http://localhost:5000")
    print("Press Ctrl+C to stop")
    print("=" * 60 + "\n")
    
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)
